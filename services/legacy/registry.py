"""SQLite Legacy registry for local and single-node deployments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from services.digital_self.compiler import canonical_manifest_bytes
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfVersion,
    ManifestEntry,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
)
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
from services.self_model.domain import RelationshipProfile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS legacy_grants (
    grant_id TEXT PRIMARY KEY,
    owner_account_id TEXT NOT NULL,
    grantee_account_id TEXT NOT NULL CHECK (grantee_account_id <> owner_account_id),
    version_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    relationship_profile_id TEXT NOT NULL,
    relationship_profile_version INTEGER NOT NULL CHECK (relationship_profile_version > 0),
    relationship_id TEXT NOT NULL,
    relationship_salutation TEXT NOT NULL,
    relationship_tone TEXT NOT NULL,
    relationship_advice_style TEXT NOT NULL,
    relationship_sharing_scope TEXT NOT NULL,
    relationship_boundaries_json TEXT NOT NULL,
    allowed_items_json TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('family', 'public')),
    scope_sha256 TEXT NOT NULL CHECK (length(scope_sha256) = 64),
    voice_allowed INTEGER NOT NULL CHECK (voice_allowed IN (0, 1)),
    expires_at TEXT NOT NULL,
    activated_at TEXT,
    revoked_at TEXT,
    grant_snapshot_sha256 TEXT NOT NULL CHECK (length(grant_snapshot_sha256) = 64),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legacy_grants_owner ON legacy_grants(owner_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_legacy_grants_grantee ON legacy_grants(grantee_account_id, created_at);

CREATE TRIGGER IF NOT EXISTS legacy_grant_immutable
BEFORE UPDATE OF owner_account_id, grantee_account_id, version_id, version_number,
                 manifest_sha256, relationship_profile_id,
                 relationship_profile_version, relationship_id,
                 relationship_salutation, relationship_tone,
                 relationship_advice_style, relationship_sharing_scope,
                 relationship_boundaries_json, allowed_items_json, visibility,
                 scope_sha256, voice_allowed, expires_at, created_at
ON legacy_grants
BEGIN SELECT RAISE(ABORT, 'legacy grant snapshot is immutable'); END;

CREATE TABLE IF NOT EXISTS legacy_relationship_shells (
    shell_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL UNIQUE REFERENCES legacy_grants(grant_id) ON DELETE CASCADE,
    owner_account_id TEXT NOT NULL,
    grantee_account_id TEXT NOT NULL,
    preferences_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS legacy_shell_identity_immutable
BEFORE UPDATE OF grant_id, owner_account_id, grantee_account_id, created_at
ON legacy_relationship_shells
BEGIN SELECT RAISE(ABORT, 'legacy shell identity is immutable'); END;

CREATE TABLE IF NOT EXISTS legacy_shell_turns (
    shell_turn_id TEXT PRIMARY KEY,
    shell_id TEXT NOT NULL REFERENCES legacy_relationship_shells(shell_id) ON DELETE CASCADE,
    grant_id TEXT NOT NULL REFERENCES legacy_grants(grant_id) ON DELETE CASCADE,
    actor_role TEXT NOT NULL CHECK (actor_role IN ('grantee', 'digital_self')),
    actual_heard_text TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    tool_epoch INTEGER NOT NULL CHECK (tool_epoch >= 0),
    occurred_at TEXT NOT NULL,
    UNIQUE (shell_id, actor_role, session_id, turn_id, generation_id, tool_epoch)
);
CREATE TRIGGER IF NOT EXISTS legacy_shell_turn_immutable
BEFORE UPDATE ON legacy_shell_turns
BEGIN SELECT RAISE(ABORT, 'legacy shell turns are immutable'); END;

CREATE TABLE IF NOT EXISTS legacy_audit_events (
    event_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES legacy_grants(grant_id) ON DELETE CASCADE,
    owner_account_id TEXT NOT NULL,
    grantee_account_id TEXT NOT NULL,
    actor_account_id TEXT NOT NULL,
    shell_id TEXT,
    shell_turn_id TEXT,
    session_id TEXT,
    turn_id TEXT,
    generation_id TEXT,
    tool_epoch INTEGER CHECK (tool_epoch IS NULL OR tool_epoch >= 0),
    target_kind TEXT CHECK (target_kind IS NULL OR target_kind IN (
        'memory_claim', 'persona_trait', 'cognitive_claim', 'decision_case',
        'relationship_profile', 'voice_profile'
    )),
    target_id TEXT,
    action TEXT NOT NULL CHECK (action IN (
        'issue', 'activate', 'revoke', 'resolve_access',
        'append_shell_turn', 'update_shell_preferences', 'read_source',
        'plan_answer', 'select_voice', 'refuse'
    )),
    decision TEXT NOT NULL CHECK (decision IN ('allowed', 'denied')),
    reason TEXT NOT NULL CHECK (reason IN (
        'issued', 'activated', 'revoked', 'owner_preview', 'grantee_session',
        'shell_turn_appended', 'shell_preferences_updated', 'source_read',
        'answer_planned', 'voice_selected', 'privacy_refusal', 'unknown_refusal'
    )),
    occurred_at TEXT NOT NULL,
    CHECK (
        (session_id IS NULL AND turn_id IS NULL AND generation_id IS NULL AND tool_epoch IS NULL)
        OR
        (session_id IS NOT NULL AND turn_id IS NOT NULL
         AND generation_id IS NOT NULL AND tool_epoch IS NOT NULL)
    ),
    CHECK (
        (target_kind IS NULL AND target_id IS NULL)
        OR (target_kind IS NOT NULL AND target_id IS NOT NULL)
    ),
    CHECK (
        (action = 'issue' AND decision = 'allowed' AND reason = 'issued')
        OR (action = 'activate' AND decision = 'allowed' AND reason = 'activated')
        OR (action = 'revoke' AND decision = 'allowed' AND reason = 'revoked')
        OR (action = 'resolve_access' AND decision = 'allowed'
            AND reason IN ('owner_preview', 'grantee_session'))
        OR (action = 'append_shell_turn' AND decision = 'allowed'
            AND reason = 'shell_turn_appended')
        OR (action = 'update_shell_preferences' AND decision = 'allowed'
            AND reason = 'shell_preferences_updated')
        OR (action = 'read_source' AND decision = 'allowed' AND reason = 'source_read'
            AND session_id IS NOT NULL AND target_kind <> 'voice_profile')
        OR (action = 'plan_answer' AND decision = 'allowed' AND reason = 'answer_planned'
            AND session_id IS NOT NULL AND target_kind IS NULL)
        OR (action = 'select_voice' AND decision = 'allowed' AND reason = 'voice_selected'
            AND target_kind = 'voice_profile')
        OR (action = 'refuse' AND decision = 'denied'
            AND reason IN ('privacy_refusal', 'unknown_refusal')
            AND session_id IS NOT NULL AND target_kind IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_legacy_audit_grant_occurred
ON legacy_audit_events(grant_id, occurred_at);
CREATE TRIGGER IF NOT EXISTS legacy_audit_event_immutable
BEFORE UPDATE ON legacy_audit_events
BEGIN SELECT RAISE(ABORT, 'legacy audit events are immutable'); END;

CREATE TABLE IF NOT EXISTS legacy_command_receipts (
    actor_account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_type TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK (length(command_sha256) = 64),
    result_kind TEXT NOT NULL CHECK (result_kind IN ('grant', 'shell', 'shell_turn')),
    result_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (actor_account_id, idempotency_key)
);
"""

_PREFERENCE_VALUES = {
    "preferred_response_length": {"brief", "balanced", "detailed"},
    "question_frequency": {"rare", "occasional"},
}
_DEFAULT_PREFERENCES = (
    ("preferred_response_length", "balanced"),
    ("question_frequency", "occasional"),
)


class LegacyRegistry:
    def __init__(self, sqlite_path: Path) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(cls, path: str | Path) -> LegacyRegistry:
        return cls(Path(path))

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            with sqlite3.connect(self._path, timeout=5) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._receipt_result(
                connection, owner_account_id, idempotency_key, "issue", command
            )
            if duplicate is not None:
                return self._grant_from_row(self._grant_row(connection, duplicate))
            grant_id = str(uuid.uuid4())
            snapshot_sha256 = _grant_digest(
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
            connection.execute(
                """
                INSERT INTO legacy_grants (
                    grant_id, owner_account_id, grantee_account_id,
                    version_id, version_number, manifest_sha256,
                    relationship_profile_id, relationship_profile_version,
                    relationship_id, relationship_salutation, relationship_tone,
                    relationship_advice_style, relationship_sharing_scope,
                    relationship_boundaries_json,
                    allowed_items_json, visibility, scope_sha256, voice_allowed,
                    expires_at, activated_at, revoked_at, grant_snapshot_sha256,
                    revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 1, ?)
                """,
                (
                    grant_id,
                    owner_account_id,
                    grantee.account_id,
                    version.version_id,
                    version.version_number,
                    version.manifest_sha256,
                    relationship_profile.profile_id,
                    relationship_profile.version_number,
                    relationship_profile.relationship_id,
                    relationship_profile.salutation,
                    relationship_profile.tone,
                    relationship_profile.advice_style,
                    relationship_profile.sharing_scope,
                    _json(list(relationship_profile.boundaries)),
                    _json([_item_dict(item) for item in items]),
                    visibility,
                    scope_sha256,
                    int(voice_allowed),
                    expires_at.isoformat(),
                    snapshot_sha256,
                    now.isoformat(),
                ),
            )
            self._audit(
                connection,
                grant_id=grant_id,
                owner_account_id=owner_account_id,
                grantee_account_id=grantee.account_id,
                actor_account_id=owner_account_id,
                action="issue",
                reason="issued",
                now=now,
            )
            self._save_receipt(
                connection,
                actor_account_id=owner_account_id,
                idempotency_key=idempotency_key,
                command_type="issue",
                command=command,
                result_kind="grant",
                result_id=grant_id,
                now=now,
            )
            return self._grant_from_row(self._grant_row(connection, grant_id))

    async def activate(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        expected_grant_snapshot_sha256: str,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyGrant:
        return self._transition(
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
        return self._transition(
            actor_account_id=actor_account_id,
            grant_id=grant_id,
            expected_grant_snapshot_sha256=expected_grant_snapshot_sha256,
            idempotency_key=idempotency_key,
            action="revoke",
            now=now,
        )

    def _transition(
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._receipt_result(
                connection, actor_account_id, idempotency_key, action, command
            )
            if duplicate is not None:
                return self._grant_from_row(self._grant_row(connection, duplicate))
            grant = self._visible_grant(connection, actor_account_id, grant_id)
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
            connection.execute(
                """
                UPDATE legacy_grants
                SET activated_at = ?, revoked_at = ?, grant_snapshot_sha256 = ?, revision = ?
                WHERE grant_id = ?
                """,
                (
                    activated_at.isoformat() if activated_at is not None else None,
                    revoked_at.isoformat() if revoked_at is not None else None,
                    snapshot_sha256,
                    revision,
                    grant_id,
                ),
            )
            self._audit(
                connection,
                grant_id=grant_id,
                owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                actor_account_id=actor_account_id,
                action=action,
                reason="activated" if action == "activate" else "revoked",
                now=now,
            )
            self._save_receipt(
                connection,
                actor_account_id=actor_account_id,
                idempotency_key=idempotency_key,
                command_type=action,
                command=command,
                result_kind="grant",
                result_id=grant_id,
                now=now,
            )
            return self._grant_from_row(self._grant_row(connection, grant_id))

    async def resolve_access(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        purpose: LegacyAccessPurpose,
        now: datetime,
    ) -> LegacyAccessSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            grant = self._visible_grant(connection, actor_account_id, grant_id)
            if grant.status_at(now) in {"expired", "revoked"}:
                raise LegacyAccessDeniedError("legacy grant is not available")
            shell_id: str | None = None
            actor_role: LegacyActorRole
            if purpose == "owner_preview":
                if actor_account_id != grant.owner_account_id:
                    raise LegacyNotFoundError(grant_id)
                actor_role = "owner_preview"
            elif purpose == "grantee_session":
                if actor_account_id != grant.grantee_account_id:
                    raise LegacyNotFoundError(grant_id)
                if grant.status_at(now) != "active":
                    raise LegacyAccessDeniedError("legacy grant is not active")
                actor_role = "grantee"
                shell_id = self._ensure_shell(connection, grant, now).shell_id
            else:
                raise ValueError("unknown Legacy access purpose")
            self._audit(
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._receipt_result(
                connection, actor_account_id, idempotency_key, "append_shell_turn", command
            )
            if duplicate is not None:
                return self._turn_from_row(self._turn_row(connection, duplicate))
            shell = self._visible_shell(connection, actor_account_id, shell_id)
            if actor_account_id != shell.grantee_account_id:
                raise LegacyNotFoundError(shell_id)
            grant = self._grant_from_row(self._grant_row(connection, shell.grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            shell_turn_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO legacy_shell_turns (
                    shell_turn_id, shell_id, grant_id, actor_role, actual_heard_text,
                    session_id, turn_id, generation_id, tool_epoch, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shell_turn_id,
                    shell_id,
                    shell.grant_id,
                    actor_role,
                    actual_heard_text,
                    fence.session_id,
                    fence.turn_id,
                    fence.generation_id,
                    fence.tool_epoch,
                    now.isoformat(),
                ),
            )
            self._audit(
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
            self._save_receipt(
                connection,
                actor_account_id=actor_account_id,
                idempotency_key=idempotency_key,
                command_type="append_shell_turn",
                command=command,
                result_kind="shell_turn",
                result_id=shell_turn_id,
                now=now,
            )
            return self._turn_from_row(self._turn_row(connection, shell_turn_id))

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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            shell = self._visible_shell(connection, actor_account_id, shell_id)
            if actor_account_id != shell.grantee_account_id:
                raise LegacyNotFoundError(shell_id)
            grant = self._grant_from_row(self._grant_row(connection, shell.grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            duplicate = self._receipt_result(
                connection,
                actor_account_id,
                idempotency_key,
                "update_shell_preferences",
                command,
            )
            if duplicate is not None:
                return self._shell_from_row(self._shell_row(connection, duplicate))
            if shell.revision != expected_shell_revision:
                raise LegacyGrantConflictError("legacy shell revision changed")
            connection.execute(
                """
                UPDATE legacy_relationship_shells
                SET preferences_json = ?, revision = revision + 1, updated_at = ?
                WHERE shell_id = ?
                """,
                (_json(list(canonical)), now.isoformat(), shell_id),
            )
            self._audit(
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
            self._save_receipt(
                connection,
                actor_account_id=actor_account_id,
                idempotency_key=idempotency_key,
                command_type="update_shell_preferences",
                command=command,
                result_kind="shell",
                result_id=shell_id,
                now=now,
            )
            return self._shell_from_row(self._shell_row(connection, shell_id))

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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            grant = self._visible_grant(connection, actor_account_id, grant_id)
            _validate_runtime_audit_status(grant, actor_account_id=actor_account_id, now=now)
            shell_row = connection.execute(
                "SELECT shell_id FROM legacy_relationship_shells WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT OR IGNORE INTO legacy_audit_events (
                    event_id, grant_id, owner_account_id, grantee_account_id,
                    actor_account_id, shell_id, shell_turn_id, session_id, turn_id,
                    generation_id, tool_epoch, target_kind, target_id, action,
                    decision, reason, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    grant.grant_id,
                    grant.owner_account_id,
                    grant.grantee_account_id,
                    actor_account_id,
                    str(shell_row["shell_id"]) if shell_row is not None else None,
                    fence.session_id if fence is not None else None,
                    fence.turn_id if fence is not None else None,
                    fence.generation_id if fence is not None else None,
                    fence.tool_epoch if fence is not None else None,
                    target.kind if target is not None else None,
                    target.target_id if target is not None else None,
                    action,
                    decision,
                    reason,
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM legacy_audit_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return self._audit_from_row(cast(sqlite3.Row, row))

    async def get_grant(self, *, actor_account_id: str, grant_id: str) -> LegacyGrant:
        with self._connect() as connection:
            return self._visible_grant(connection, actor_account_id, grant_id)

    async def list_grants(
        self,
        *,
        actor_account_id: str,
        role: Literal["owner", "grantee"],
    ) -> tuple[LegacyGrant, ...]:
        if role not in {"owner", "grantee"}:
            raise ValueError("unknown Legacy grant role")
        column = "owner_account_id" if role == "owner" else "grantee_account_id"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM legacy_grants WHERE {column} = ? ORDER BY created_at, grant_id",
                (actor_account_id,),
            ).fetchall()
            return tuple(self._grant_from_row(row) for row in rows)

    async def get_shell(
        self, *, actor_account_id: str, shell_id: str, now: datetime
    ) -> LegacyRelationshipShell:
        with self._connect() as connection:
            shell = self._visible_shell(connection, actor_account_id, shell_id)
            grant = self._grant_from_row(self._grant_row(connection, shell.grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            return shell

    async def get_shell_for_grant(
        self, *, actor_account_id: str, grant_id: str, now: datetime
    ) -> LegacyRelationshipShell | None:
        with self._connect() as connection:
            grant = self._visible_grant(connection, actor_account_id, grant_id)
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            row = connection.execute(
                "SELECT * FROM legacy_relationship_shells WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            return self._shell_from_row(row) if row is not None else None

    async def list_audit_events(
        self, *, actor_account_id: str, grant_id: str
    ) -> tuple[LegacyAuditEvent, ...]:
        with self._connect() as connection:
            self._visible_grant(connection, actor_account_id, grant_id)
            rows = connection.execute(
                "SELECT * FROM legacy_audit_events WHERE grant_id = ? ORDER BY occurred_at, event_id",
                (grant_id,),
            ).fetchall()
            return tuple(self._audit_from_row(row) for row in rows)

    async def export_for_account(self, *, account_id: str) -> LegacyAccountExport:
        with self._connect() as connection:
            grants = connection.execute(
                """
                SELECT * FROM legacy_grants
                WHERE owner_account_id = ? OR grantee_account_id = ?
                ORDER BY created_at, grant_id
                """,
                (account_id, account_id),
            ).fetchall()
            grant_ids = tuple(str(row["grant_id"]) for row in grants)
            if not grant_ids:
                return LegacyAccountExport((), (), (), ())
            placeholders = ",".join("?" for _ in grant_ids)
            shells = connection.execute(
                f"SELECT * FROM legacy_relationship_shells WHERE grant_id IN ({placeholders})",
                grant_ids,
            ).fetchall()
            turns = connection.execute(
                f"SELECT * FROM legacy_shell_turns WHERE grant_id IN ({placeholders})",
                grant_ids,
            ).fetchall()
            events = connection.execute(
                f"SELECT * FROM legacy_audit_events WHERE grant_id IN ({placeholders})",
                grant_ids,
            ).fetchall()
            return LegacyAccountExport(
                tuple(self._grant_from_row(row) for row in grants),
                tuple(self._shell_from_row(row) for row in shells),
                tuple(self._turn_from_row(row) for row in turns),
                tuple(self._audit_from_row(row) for row in events),
            )

    async def delete_for_account(self, *, account_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM legacy_command_receipts
                WHERE actor_account_id = ?
                   OR (result_kind = 'grant' AND result_id IN (
                        SELECT grant_id FROM legacy_grants
                        WHERE owner_account_id = ? OR grantee_account_id = ?
                   ))
                   OR (result_kind = 'shell' AND result_id IN (
                        SELECT shell_id FROM legacy_relationship_shells
                        WHERE owner_account_id = ? OR grantee_account_id = ?
                   ))
                   OR (result_kind = 'shell_turn' AND result_id IN (
                        SELECT turn.shell_turn_id
                        FROM legacy_shell_turns AS turn
                        JOIN legacy_grants AS grant ON grant.grant_id = turn.grant_id
                        WHERE grant.owner_account_id = ? OR grant.grantee_account_id = ?
                   ))
                """,
                (account_id,) * 7,
            )
            connection.execute(
                "DELETE FROM legacy_grants WHERE owner_account_id = ? OR grantee_account_id = ?",
                (account_id, account_id),
            )

    def _ensure_shell(
        self,
        connection: sqlite3.Connection,
        grant: LegacyGrant,
        now: datetime,
    ) -> LegacyRelationshipShell:
        row = connection.execute(
            "SELECT * FROM legacy_relationship_shells WHERE grant_id = ?", (grant.grant_id,)
        ).fetchone()
        if row is None:
            shell_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO legacy_relationship_shells (
                    shell_id, grant_id, owner_account_id, grantee_account_id,
                    preferences_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    shell_id,
                    grant.grant_id,
                    grant.owner_account_id,
                    grant.grantee_account_id,
                    _json(list(_DEFAULT_PREFERENCES)),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = self._shell_row(connection, shell_id)
        return self._shell_from_row(cast(sqlite3.Row, row))

    @staticmethod
    def _visible_grant(
        connection: sqlite3.Connection, actor_account_id: str, grant_id: str
    ) -> LegacyGrant:
        row = connection.execute(
            """
            SELECT * FROM legacy_grants WHERE grant_id = ?
              AND (owner_account_id = ? OR grantee_account_id = ?)
            """,
            (grant_id, actor_account_id, actor_account_id),
        ).fetchone()
        if row is None:
            raise LegacyNotFoundError(grant_id)
        return LegacyRegistry._grant_from_row(cast(sqlite3.Row, row))

    @staticmethod
    def _visible_shell(
        connection: sqlite3.Connection, actor_account_id: str, shell_id: str
    ) -> LegacyRelationshipShell:
        row = connection.execute(
            """
            SELECT * FROM legacy_relationship_shells WHERE shell_id = ?
              AND (owner_account_id = ? OR grantee_account_id = ?)
            """,
            (shell_id, actor_account_id, actor_account_id),
        ).fetchone()
        if row is None:
            raise LegacyNotFoundError(shell_id)
        return LegacyRegistry._shell_from_row(cast(sqlite3.Row, row))

    @staticmethod
    def _grant_row(connection: sqlite3.Connection, grant_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM legacy_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            raise LegacyNotFoundError(grant_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _shell_row(connection: sqlite3.Connection, shell_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM legacy_relationship_shells WHERE shell_id = ?", (shell_id,)
        ).fetchone()
        if row is None:
            raise LegacyNotFoundError(shell_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _turn_row(connection: sqlite3.Connection, shell_turn_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM legacy_shell_turns WHERE shell_turn_id = ?", (shell_turn_id,)
        ).fetchone()
        if row is None:
            raise LegacyNotFoundError(shell_turn_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _receipt_result(
        connection: sqlite3.Connection,
        actor_account_id: str,
        idempotency_key: str,
        command_type: str,
        command: object,
    ) -> str | None:
        _required(idempotency_key, "idempotency_key", 128)
        row = connection.execute(
            """
            SELECT * FROM legacy_command_receipts
            WHERE actor_account_id = ? AND idempotency_key = ?
            """,
            (actor_account_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["command_type"]) != command_type or str(row["command_sha256"]) != _digest(
            command
        ):
            raise LegacyIdempotencyConflictError("Legacy idempotency key was reused")
        return str(row["result_id"])

    @staticmethod
    def _save_receipt(
        connection: sqlite3.Connection,
        *,
        actor_account_id: str,
        idempotency_key: str,
        command_type: str,
        command: object,
        result_kind: str,
        result_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO legacy_command_receipts (
                actor_account_id, idempotency_key, command_type, command_sha256,
                result_kind, result_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_account_id,
                idempotency_key,
                command_type,
                _digest(command),
                result_kind,
                result_id,
                now.isoformat(),
            ),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
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
        decision: LegacyAuditDecision = "allowed",
    ) -> None:
        connection.execute(
            """
            INSERT INTO legacy_audit_events (
                event_id, grant_id, owner_account_id, grantee_account_id,
                actor_account_id, shell_id, shell_turn_id, action, decision,
                reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                grant_id,
                owner_account_id,
                grantee_account_id,
                actor_account_id,
                shell_id,
                shell_turn_id,
                action,
                decision,
                reason,
                now.isoformat(),
            ),
        )

    @staticmethod
    def _grant_from_row(row: sqlite3.Row) -> LegacyGrant:
        raw_items = json.loads(str(row["allowed_items_json"]))
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
                boundaries=tuple(json.loads(str(row["relationship_boundaries_json"]))),
            ),
            allowed_items=tuple(
                LegacyManifestItemRef(
                    kind=cast(LegacyManifestItemKind, str(item["kind"])),
                    item_id=str(item["item_id"]),
                )
                for item in raw_items
            ),
            visibility=cast(LegacyVisibility, str(row["visibility"])),
            scope_sha256=str(row["scope_sha256"]),
            voice_allowed=bool(row["voice_allowed"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            activated_at=_optional_datetime(row["activated_at"]),
            revoked_at=_optional_datetime(row["revoked_at"]),
            grant_snapshot_sha256=str(row["grant_snapshot_sha256"]),
            revision=int(row["revision"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _shell_from_row(row: sqlite3.Row) -> LegacyRelationshipShell:
        preferences = json.loads(str(row["preferences_json"]))
        return LegacyRelationshipShell(
            shell_id=str(row["shell_id"]),
            grant_id=str(row["grant_id"]),
            owner_account_id=str(row["owner_account_id"]),
            grantee_account_id=str(row["grantee_account_id"]),
            preferences=tuple((str(key), str(value)) for key, value in preferences),
            revision=int(row["revision"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> LegacyShellTurn:
        return LegacyShellTurn(
            shell_turn_id=str(row["shell_turn_id"]),
            shell_id=str(row["shell_id"]),
            grant_id=str(row["grant_id"]),
            actor_role=cast(LegacyShellActorRole, str(row["actor_role"])),
            actual_heard_text=str(row["actual_heard_text"]),
            fence=LegacyFence(
                session_id=str(row["session_id"]),
                turn_id=str(row["turn_id"]),
                generation_id=str(row["generation_id"]),
                tool_epoch=int(row["tool_epoch"]),
            ),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        )

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> LegacyAuditEvent:
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
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        )


def _runtime_audit_event_id(
    *,
    actor_account_id: str,
    grant_id: str,
    action: LegacyRuntimeAuditAction,
    decision: LegacyAuditDecision,
    reason: LegacyAuditReason,
    fence: LegacyFence | None,
    target: LegacyAuditTarget | None,
) -> str:
    _required(actor_account_id, "actor_account_id")
    _required(grant_id, "grant_id")
    valid = {
        ("read_source", "allowed", "source_read"),
        ("plan_answer", "allowed", "answer_planned"),
        ("select_voice", "allowed", "voice_selected"),
        ("refuse", "denied", "privacy_refusal"),
        ("refuse", "denied", "unknown_refusal"),
    }
    if (action, decision, reason) not in valid:
        raise ValueError("invalid Legacy runtime audit action, decision, and reason")
    if action != "select_voice" and fence is None:
        raise ValueError("Legacy runtime audit fence is required")
    if fence is not None:
        _validate_fence(fence)
    if action in {"read_source", "select_voice"}:
        if target is None:
            raise ValueError("Legacy runtime audit target is required")
        _required(target.target_id, "target_id", 128)
        if action == "read_source" and target.kind == "voice_profile":
            raise ValueError("Legacy source audit target must be a manifest item")
        if action == "select_voice" and target.kind != "voice_profile":
            raise ValueError("Legacy voice audit target must be a voice profile")
    elif target is not None:
        raise ValueError("Legacy runtime audit target is not allowed")
    canonical = {
        "actor_account_id": actor_account_id,
        "grant_id": grant_id,
        "action": action,
        "decision": decision,
        "reason": reason,
        "fence": (
            {
                "session_id": fence.session_id,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
            }
            if fence is not None
            else None
        ),
        "target": (
            {"kind": target.kind, "target_id": target.target_id}
            if target is not None
            else None
        ),
    }
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "memoria:legacy-runtime-audit:" + _json(canonical)))


def _validate_fence(fence: LegacyFence) -> None:
    _required(fence.session_id, "session_id", 128)
    _required(fence.turn_id, "turn_id", 128)
    _required(fence.generation_id, "generation_id", 128)
    if fence.tool_epoch < 0:
        raise ValueError("tool_epoch must be non-negative")


def _validate_runtime_audit_status(
    grant: LegacyGrant, *, actor_account_id: str, now: datetime
) -> None:
    status = grant.status_at(now)
    if status in {"expired", "revoked"} or (
        actor_account_id == grant.grantee_account_id and status != "active"
    ):
        raise LegacyAccessDeniedError("legacy grant is not available")


def _validate_issue(
    *,
    owner_account_id: str,
    grantee: RegisteredGranteeSnapshot,
    version: DigitalSelfVersion,
    relationship_profile: RelationshipProfile,
    allowed_items: tuple[LegacyManifestItemRef, ...],
    visibility: object,
    expires_at: datetime,
    now: datetime,
) -> None:
    _required(owner_account_id, "owner_account_id")
    _required(grantee.account_id, "grantee account_id")
    if owner_account_id == grantee.account_id:
        raise ValueError("Legacy owner and grantee must differ")
    if grantee.registered_at > now:
        raise ValueError("grantee registration snapshot is in the future")
    if version.account_id != owner_account_id or version.status != "frozen":
        raise ValueError("Legacy requires the owner's frozen Digital Self version")
    if hashlib.sha256(canonical_manifest_bytes(version.manifest)).hexdigest() != version.manifest_sha256:
        raise ValueError("Legacy frozen manifest digest does not match its snapshot")
    if relationship_profile.account_id != owner_account_id or relationship_profile.status != "approved":
        raise ValueError("Legacy requires the owner's approved relationship profile")
    if relationship_profile.sharing_scope not in {"family", "public"}:
        raise ValueError("Legacy relationship scope must not be private or unknown")
    if relationship_profile.sharing_scope == "family" and visibility == "public":
        raise ValueError("Legacy grant visibility exceeds relationship scope")
    if visibility not in {"family", "public"}:
        raise ValueError("Legacy visibility must be family or public")
    if expires_at <= now:
        raise ValueError("Legacy grant expiry must be in the future")
    if not allowed_items:
        raise ValueError("Legacy grant scope cannot be empty")
    selected = set(allowed_items)
    if len(selected) != len(allowed_items):
        raise ValueError("Legacy scope must contain unique canonical frozen manifest items")
    for item in selected:
        matching_items = [
            entry for entry in version.manifest.entries if _entry_ref(entry) == item
        ]
        if len(matching_items) != 1:
            raise ValueError("Legacy scope must contain unique canonical frozen manifest items")
        if not _scope_allows_visibility(_entry_scope(matching_items[0]), visibility):
            raise ValueError("Legacy scope contains private, unknown, or over-visible items")
    relation_ref = LegacyManifestItemRef(
        kind="relationship_profile", item_id=relationship_profile.profile_id
    )
    matching_entries = [
        entry
        for entry in version.manifest.entries
        if _entry_ref(entry) == relation_ref
        and isinstance(entry, RelationshipProfileManifestEntry)
        and entry.version_number == relationship_profile.version_number
    ]
    if len(matching_entries) != 1 or not _relationship_matches_manifest(
        relationship_profile, matching_entries[0]
    ):
        raise ValueError("approved relationship snapshot is not exact in frozen manifest")


def _entry_ref(entry: ManifestEntry) -> LegacyManifestItemRef:
    if isinstance(entry, MemoryClaimManifestEntry):
        return LegacyManifestItemRef("memory_claim", entry.claim_id)
    if isinstance(entry, PersonaTraitManifestEntry):
        return LegacyManifestItemRef("persona_trait", entry.trait_id)
    if isinstance(entry, CognitiveClaimManifestEntry):
        return LegacyManifestItemRef("cognitive_claim", entry.claim_id)
    if isinstance(entry, DecisionCaseManifestEntry):
        return LegacyManifestItemRef("decision_case", entry.case_id)
    return LegacyManifestItemRef("relationship_profile", entry.profile_id)


def _entry_scope(entry: ManifestEntry) -> str | None:
    if isinstance(entry, MemoryClaimManifestEntry):
        return entry.sensitive_domain
    if isinstance(entry, PersonaTraitManifestEntry):
        return None
    return entry.sharing_scope


def _scope_allows_visibility(scope: str | None, visibility: object) -> bool:
    return scope == "public" or (scope == "family" and visibility == "family")


def _relationship_matches_manifest(
    profile: RelationshipProfile, entry: RelationshipProfileManifestEntry
) -> bool:
    return (
        profile.profile_id,
        profile.version_number,
        profile.person_id,
        profile.relationship_id,
        profile.salutation,
        profile.tone,
        profile.advice_style,
        profile.sharing_scope,
        profile.boundaries,
    ) == (
        entry.profile_id,
        entry.version_number,
        entry.person_id,
        entry.relationship_id,
        entry.salutation,
        entry.tone,
        entry.advice_style,
        entry.sharing_scope,
        entry.boundaries,
    )


def _canonical_items(
    items: tuple[LegacyManifestItemRef, ...],
) -> tuple[LegacyManifestItemRef, ...]:
    return tuple(sorted(items, key=lambda item: (item.kind, item.item_id)))


def _item_dict(item: LegacyManifestItemRef) -> dict[str, str]:
    return {"kind": item.kind, "item_id": item.item_id}


def _validate_turn(text: str, fence: LegacyFence) -> None:
    _required(text, "actual_heard_text", 20_000)
    _required(fence.session_id, "session_id", 256)
    _required(fence.turn_id, "turn_id", 256)
    _required(fence.generation_id, "generation_id", 256)
    if fence.tool_epoch < 0:
        raise ValueError("tool_epoch must be non-negative")


def _validate_preferences(
    preferences: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if len(preferences) > len(_PREFERENCE_VALUES):
        raise ValueError("too many Legacy shell preferences")
    canonical = tuple(sorted(preferences))
    if len({key for key, _ in canonical}) != len(canonical):
        raise ValueError("duplicate Legacy shell preference")
    if {key for key, _ in canonical} != set(_PREFERENCE_VALUES):
        raise ValueError("Legacy shell preferences must include every supported key")
    for key, value in canonical:
        if key not in _PREFERENCE_VALUES:
            raise ValueError("unknown Legacy shell preference")
        if value not in _PREFERENCE_VALUES[key]:
            raise ValueError("unknown Legacy shell preference value")
    return canonical


def _grant_digest(
    *,
    grant_id: str,
    owner_account_id: str,
    grantee_account_id: str,
    version_id: str,
    version_number: int,
    manifest_sha256: str,
    relationship_profile_id: str,
    relationship_profile_version: int,
    relationship: LegacyRelationshipSnapshot,
    scope_sha256: str,
    voice_allowed: bool,
    expires_at: datetime,
    activated_at: datetime | None,
    revoked_at: datetime | None,
    revision: int,
    created_at: datetime,
) -> str:
    return _digest(
        {
            "grant_id": grant_id,
            "owner_account_id": owner_account_id,
            "grantee_account_id": grantee_account_id,
            "version_id": version_id,
            "version_number": version_number,
            "manifest_sha256": manifest_sha256,
            "relationship_profile_id": relationship_profile_id,
            "relationship_profile_version": relationship_profile_version,
            "relationship": {
                "relationship_id": relationship.relationship_id,
                "salutation": relationship.salutation,
                "tone": relationship.tone,
                "advice_style": relationship.advice_style,
                "sharing_scope": relationship.sharing_scope,
                "boundaries": list(relationship.boundaries),
            },
            "scope_sha256": scope_sha256,
            "voice_allowed": voice_allowed,
            "expires_at": expires_at.isoformat(),
            "activated_at": activated_at.isoformat() if activated_at else None,
            "revoked_at": revoked_at.isoformat() if revoked_at else None,
            "revision": revision,
            "created_at": created_at.isoformat(),
        }
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required(value: str, name: str, maximum: int = 256) -> None:
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is required and must be at most {maximum} characters")


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None
