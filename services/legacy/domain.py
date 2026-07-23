"""Pure Legacy grant, access, shell, and audit contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from services.digital_self.domain import DigitalSelfVersion
from services.self_model.domain import RelationshipProfile

LegacyGrantStatus = Literal["pending", "active", "expired", "revoked"]
LegacyActorRole = Literal["owner_preview", "grantee"]
LegacyShellActorRole = Literal["grantee", "digital_self"]
LegacyAccessPurpose = Literal["owner_preview", "grantee_session"]
LegacyVisibility = Literal["family", "public"]
LegacyManifestItemKind = Literal[
    "memory_claim",
    "persona_trait",
    "cognitive_claim",
    "decision_case",
    "relationship_profile",
]
LegacyRuntimeAuditAction = Literal["read_source", "plan_answer", "select_voice", "refuse"]
LegacyAuditAction = Literal[
    "issue",
    "activate",
    "revoke",
    "resolve_access",
    "append_shell_turn",
    "update_shell_preferences",
    "read_source",
    "plan_answer",
    "select_voice",
    "refuse",
]
LegacyAuditDecision = Literal["allowed", "denied"]
LegacyAuditReason = Literal[
    "issued",
    "activated",
    "revoked",
    "owner_preview",
    "grantee_session",
    "shell_turn_appended",
    "shell_preferences_updated",
    "source_read",
    "answer_planned",
    "voice_selected",
    "privacy_refusal",
    "unknown_refusal",
]
LegacyAuditTargetKind = Literal[
    "memory_claim",
    "persona_trait",
    "cognitive_claim",
    "decision_case",
    "relationship_profile",
    "voice_profile",
]


class LegacyNotFoundError(LookupError):
    """The resource is absent from the actor's Legacy account scope."""


class LegacyAccessDeniedError(PermissionError):
    """The actor or requested access purpose is not allowed."""


class LegacyGrantConflictError(RuntimeError):
    """The expected immutable grant snapshot is stale."""


class LegacyIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different Legacy command."""


@dataclass(frozen=True, slots=True)
class RegisteredGranteeSnapshot:
    """Minimal snapshot accepted from an upstream registration authority."""

    account_id: str
    registered_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyManifestItemRef:
    kind: LegacyManifestItemKind
    item_id: str


@dataclass(frozen=True, slots=True)
class LegacyRelationshipSnapshot:
    profile_id: str
    version_number: int
    relationship_id: str
    salutation: str
    tone: str
    advice_style: str
    sharing_scope: str
    boundaries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyGrant:
    grant_id: str
    owner_account_id: str
    grantee_account_id: str
    version_id: str
    version_number: int
    manifest_sha256: str
    relationship: LegacyRelationshipSnapshot
    allowed_items: tuple[LegacyManifestItemRef, ...]
    visibility: LegacyVisibility
    scope_sha256: str
    voice_allowed: bool
    expires_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None
    grant_snapshot_sha256: str
    revision: int
    created_at: datetime

    def status_at(self, at: datetime) -> LegacyGrantStatus:
        if self.revoked_at is not None:
            return "revoked"
        if at >= self.expires_at:
            return "expired"
        if self.activated_at is None:
            return "pending"
        return "active"


@dataclass(frozen=True, slots=True)
class LegacyAccessSnapshot:
    actor_role: LegacyActorRole
    resource_owner_account_id: str
    grantee_account_id: str
    grant_id: str
    shell_id: str | None
    version_id: str
    version_number: int
    manifest_sha256: str
    grant_snapshot_sha256: str
    scope_sha256: str
    allowed_items: tuple[LegacyManifestItemRef, ...]
    relationship_profile_id: str
    relationship_profile_version: int
    voice_allowed: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyRelationshipShell:
    shell_id: str
    grant_id: str
    owner_account_id: str
    grantee_account_id: str
    preferences: tuple[tuple[str, str], ...]
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyFence:
    session_id: str
    turn_id: str
    generation_id: str
    tool_epoch: int


@dataclass(frozen=True, slots=True)
class LegacyAuditTarget:
    kind: LegacyAuditTargetKind
    target_id: str


@dataclass(frozen=True, slots=True)
class LegacyShellTurn:
    shell_turn_id: str
    shell_id: str
    grant_id: str
    actor_role: LegacyShellActorRole
    actual_heard_text: str
    fence: LegacyFence
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyAuditEvent:
    event_id: str
    grant_id: str
    owner_account_id: str
    grantee_account_id: str
    actor_account_id: str
    shell_id: str | None
    shell_turn_id: str | None
    session_id: str | None
    turn_id: str | None
    generation_id: str | None
    tool_epoch: int | None
    target_kind: LegacyAuditTargetKind | None
    target_id: str | None
    action: LegacyAuditAction
    decision: LegacyAuditDecision
    reason: LegacyAuditReason
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class LegacyAccountExport:
    grants: tuple[LegacyGrant, ...]
    shells: tuple[LegacyRelationshipShell, ...]
    shell_turns: tuple[LegacyShellTurn, ...]
    audit_events: tuple[LegacyAuditEvent, ...]


class LegacyRegistryPort(Protocol):
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
    ) -> LegacyGrant: ...

    async def activate(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        expected_grant_snapshot_sha256: str,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyGrant: ...

    async def revoke(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        expected_grant_snapshot_sha256: str,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyGrant: ...

    async def resolve_access(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        purpose: LegacyAccessPurpose,
        now: datetime,
    ) -> LegacyAccessSnapshot: ...

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
    ) -> LegacyShellTurn: ...

    async def update_shell_preferences(
        self,
        *,
        actor_account_id: str,
        shell_id: str,
        preferences: tuple[tuple[str, str], ...],
        expected_shell_revision: int,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyRelationshipShell: ...

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
    ) -> LegacyAuditEvent: ...

    async def get_grant(
        self, *, actor_account_id: str, grant_id: str
    ) -> LegacyGrant: ...

    async def list_grants(
        self,
        *,
        actor_account_id: str,
        role: Literal["owner", "grantee"],
    ) -> tuple[LegacyGrant, ...]: ...

    async def get_shell(
        self, *, actor_account_id: str, shell_id: str, now: datetime
    ) -> LegacyRelationshipShell: ...

    async def get_shell_for_grant(
        self, *, actor_account_id: str, grant_id: str, now: datetime
    ) -> LegacyRelationshipShell | None: ...

    async def list_audit_events(
        self, *, actor_account_id: str, grant_id: str
    ) -> tuple[LegacyAuditEvent, ...]: ...

    async def export_for_account(self, *, account_id: str) -> LegacyAccountExport: ...

    async def delete_for_account(self, *, account_id: str) -> None: ...
