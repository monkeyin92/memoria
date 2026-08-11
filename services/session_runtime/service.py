"""Production Session Runtime orchestration on one caller-owned transaction.

This module is the application boundary used by Control.  It deliberately
keeps HTTP/media concerns out of Session and composes the narrow Identity,
Policy and Session PostgreSQL ports over one ``memoria_action_executor``
connection.  No successful profile escapes before its receipt batch, event and
outbox row have committed atomically.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    CapabilityValue,
    DeviceDeclaredModeValue,
    PolicyActionResourceFence,
    PolicyDecision,
    PolicyObligationSpec,
    PolicyReceiptV2,
    PurposeValue,
    RuntimeProfileSignedV2,
    RuntimeProfileV2,
    SessionEvent,
    SubjectCategoryValue,
)

from services.consent.evidence import (
    RelationshipEvidence,
    RelationshipStatusValue,
    RelationTypeValue,
)
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
)
from services.policy.context import (
    PolicyContext,
    effective_action_resource_fence,
    is_resource_scoped_action,
)
from services.policy.engine import PolicyEngine
from services.policy.evidence import (
    BindingEvidencePort,
    BindingStatusValue,
    ConsentEvidencePort,
    ConsentSnapshotEvidencePort,
    RelationshipEvidencePort,
)
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.production_wiring import (
    ConsentDiscovery,
    PostgresCurrentConsentAuthorityAdapter,
    ProductionAuthorityAdapters,
    build_session_batch_service,
)
from services.policy.receipt_store import InMemoryPolicyReceiptWriter
from services.policy.receipts import (
    PolicyReceiptConflictError,
    exact_evidence_fence_valid,
)
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
    SessionRuntimeAuthorityUnavailable,
    SessionRuntimeConflict,
    SessionRuntimeContext,
)
from services.session_runtime.profile_service import (
    PROFILE_ISSUE_DEFERRED_CAPABILITIES,
    PROFILE_ISSUE_SESSION_CAPABILITIES,
    canonical_runtime_decision_purpose,
    sign_runtime_profile_payload,
)
from services.session_runtime.service_mode_resolver import ServiceModeResolver
from services.session_runtime.subject_resolver import (
    BindingSnapshot,
    ResolveSubjectCommand,
    SubjectCandidate,
    SubjectResolution,
    SubjectResolver,
)

_DEFAULT_PROFILE_TTL = timedelta(minutes=5)
_NO_EXPIRY_SENTINEL = datetime(9999, 12, 31, tzinfo=UTC)


class PersistentSessionUnavailable(RuntimeError):
    """A required production authority is absent or unavailable."""


class PersistentSessionDenied(PermissionError):
    """The authenticated actor/binding cannot create this Session."""


@dataclass(frozen=True, slots=True)
class StartPersistentSessionCommand:
    session_id: str
    actor_id: str
    device_id: str
    expected_binding_version: int
    idempotency_key: str
    now: datetime
    requested_capabilities: tuple[CapabilityValue, ...] = ("chat",)
    candidates: tuple[SubjectCandidate, ...] = ()
    multiple_speakers: bool = False
    offline: bool = False

    def __post_init__(self) -> None:
        for value, field in (
            (self.session_id, "session_id"),
            (self.actor_id, "actor_id"),
            (self.device_id, "device_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            if not value.strip():
                raise ValueError(f"{field} must not be empty")
        if self.expected_binding_version < 1:
            raise ValueError("expected_binding_version must be positive")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not self.requested_capabilities:
            raise ValueError("requested_capabilities must not be empty")
        if len(set(self.requested_capabilities)) != len(self.requested_capabilities):
            raise ValueError("requested_capabilities must be unique")


@dataclass(frozen=True, slots=True)
class CommitToolEffectCommand:
    """One Agent-prepared effect submitted to the Session authority."""

    actor_id: str
    session_id: str
    runtime_profile: RuntimeProfileSignedV2
    policy_receipt: PolicyReceiptV2
    session_epoch: int
    generation_id: int
    turn_id: int
    tool_epoch: int
    capability: str
    purpose: str
    resource_id: str
    evidence_refs: tuple[str, ...]
    idempotency_key: str
    intent: str
    payload: dict[str, object]
    payload_sha256: str
    fence_fingerprint: str
    now: datetime

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.actor_id, "actor_id", 128),
            (self.session_id, "session_id", 128),
            (self.capability, "capability", 128),
            (self.purpose, "purpose", 128),
            (self.resource_id, "resource_id", 128),
            (self.idempotency_key, "idempotency_key", 512),
            (self.intent, "intent", 512),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} must be a bounded non-blank string")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        for numeric_value, name, minimum in (
            (self.session_epoch, "session_epoch", 1),
            (self.generation_id, "generation_id", 0),
            (self.turn_id, "turn_id", 0),
            (self.tool_epoch, "tool_epoch", 0),
        ):
            if type(numeric_value) is not int or numeric_value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not self.evidence_refs or any(
            not isinstance(item, str) or not item.strip() or len(item) > 192
            for item in self.evidence_refs
        ):
            raise ValueError("evidence_refs must contain bounded ids")
        if not re.fullmatch(r"^[a-f0-9]{64}$", self.payload_sha256):
            raise ValueError("payload_sha256 must be a sha256 digest")
        if not re.fullmatch(r"^[a-f0-9]{64}$", self.fence_fingerprint):
            raise ValueError("fence_fingerprint must be a sha256 digest")
        encoded = _canonical_json(self.payload, allow_nan=False).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.payload_sha256:
            raise ValueError("payload_sha256 does not match the canonical payload")


@dataclass(frozen=True, slots=True)
class SwitchPersistentSubjectCommand:
    session_id: str
    actor_id: str
    subject_id: str | None
    now: datetime
    requested_capabilities: tuple[CapabilityValue, ...]
    claimed_subject_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.actor_id.strip():
            raise ValueError("session_id and actor_id must not be empty")
        if self.subject_id is not None and not self.subject_id.strip():
            raise ValueError("subject_id must not be empty")
        if self.claimed_subject_id is not None and not self.claimed_subject_id.strip():
            raise ValueError("claimed_subject_id must not be empty")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not self.requested_capabilities:
            raise ValueError("requested_capabilities must not be empty")


class PersistentSessionNotFound(LookupError):
    """No RLS-visible active Session/profile exists."""


@dataclass(frozen=True, slots=True)
class _BindingRole:
    person_id: str
    role: str
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BindingSubject:
    person_id: str
    subject_category: str
    age_band: str
    revision: int


@dataclass(frozen=True, slots=True)
class _LockedBindingEvidence:
    binding_id: str
    version: int
    device_id: str
    status: BindingStatusValue
    declared_mode: DeviceDeclaredModeValue
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until


@dataclass(frozen=True, slots=True)
class _LockedBinding:
    evidence: _LockedBindingEvidence
    roles: tuple[_BindingRole, ...]
    subjects: tuple[_BindingSubject, ...]
    relationships: tuple[RelationshipEvidencePort, ...]
    account_owner_person_id: str
    policy_bundle_version: str
    persona_assignment_id: str

    @property
    def snapshot(self) -> BindingSnapshot:
        members = tuple(item.person_id for item in self.subjects)
        primary = tuple(
            sorted({item.person_id for item in self.roles if item.role == "primary_subject"})
        )
        return BindingSnapshot(
            binding_id=self.evidence.binding_id,
            device_id=self.evidence.device_id,
            binding_version=self.evidence.version,
            declared_mode=self.evidence.declared_mode,
            primary_subject_ids=primary,
            member_subject_ids=members,
        )


def _canonical_json(value: object, *, allow_nan: bool = True) -> str:
    return json.dumps(
        value,
        allow_nan=allow_nan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _tool_effect_fence_fingerprint(command: CommitToolEffectCommand) -> str:
    return _digest(
        {
            "session_id": command.session_id,
            "turn_id": command.turn_id,
            "generation_id": command.generation_id,
            "tool_epoch": command.tool_epoch,
            "session_epoch": command.session_epoch,
        }
    )


def _decode_json_object(value: object, *, name: str) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise PersistentSessionUnavailable(f"{name} is not a JSON object")
    return {str(key): item for key, item in decoded.items()}


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistentSessionUnavailable(f"{name} is unavailable")
    return value


def _required_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PersistentSessionUnavailable(f"{name} is unavailable")
    return value


def _aware_datetime(value: object, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PersistentSessionUnavailable(f"{name} is invalid") from exc
    else:
        raise PersistentSessionUnavailable(f"{name} is unavailable")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistentSessionUnavailable(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_permissions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PersistentSessionUnavailable("binding role permissions are invalid")
    return tuple(value)


def _parse_locked_binding(value: object) -> _LockedBinding:
    raw = _decode_json_object(value, name="Identity binding authority")
    binding_id = _required_string(raw.get("binding_id"), name="binding_id")
    device_id = _required_string(raw.get("device_id"), name="device_id")
    version = _required_int(raw.get("binding_version"), name="binding_version")
    declared_mode_raw = _required_string(raw.get("declared_mode"), name="declared_mode")
    if declared_mode_raw not in {
        "parent_for_child",
        "self_use",
        "child_for_parent",
        "family_shared",
    }:
        raise PersistentSessionUnavailable("declared_mode is invalid")
    declared_mode = cast(DeviceDeclaredModeValue, declared_mode_raw)
    valid_from = _aware_datetime(raw.get("valid_from"), name="valid_from")
    valid_until_raw = raw.get("valid_until")
    valid_until = (
        _NO_EXPIRY_SENTINEL
        if valid_until_raw is None
        else _aware_datetime(valid_until_raw, name="valid_until")
    )
    evidence_payload = {
        "binding_id": binding_id,
        "version": version,
        "device_id": device_id,
        "status": "active",
        "declared_mode": declared_mode,
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
    }
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list):
        raise PersistentSessionUnavailable("binding roles are unavailable")
    roles: list[_BindingRole] = []
    for item in roles_raw:
        if not isinstance(item, dict):
            raise PersistentSessionUnavailable("binding role is invalid")
        roles.append(
            _BindingRole(
                person_id=_required_string(item.get("person_id"), name="role person"),
                role=_required_string(item.get("role"), name="binding role"),
                permissions=_parse_permissions(item.get("permissions")),
            )
        )
    subjects_raw = raw.get("subjects")
    if not isinstance(subjects_raw, list):
        raise PersistentSessionUnavailable("binding subjects are unavailable")
    subjects: list[_BindingSubject] = []
    for item in subjects_raw:
        if not isinstance(item, dict):
            raise PersistentSessionUnavailable("binding subject is invalid")
        category = _required_string(item.get("subject_category"), name="subject category")
        age_band = _required_string(item.get("age_band"), name="subject age band")
        if category not in {"unknown", "minor", "adult"} or age_band not in {
            "unknown",
            "under_14",
            "14_17",
            "adult",
        }:
            raise PersistentSessionUnavailable("subject classification is invalid")
        subjects.append(
            _BindingSubject(
                person_id=_required_string(item.get("person_id"), name="subject person"),
                subject_category=category,
                age_band=age_band,
                revision=_required_int(item.get("revision"), name="subject revision"),
            )
        )
    relationships_raw = raw.get("relationships")
    if not isinstance(relationships_raw, list):
        raise PersistentSessionUnavailable("binding relationships are unavailable")
    relationships: list[RelationshipEvidencePort] = []
    for item in relationships_raw:
        if not isinstance(item, dict):
            raise PersistentSessionUnavailable("binding relationship is invalid")
        updated_at = _aware_datetime(item.get("updated_at"), name="relationship update")
        valid_from = _aware_datetime(item.get("valid_from"), name="relationship start")
        valid_until_raw = item.get("valid_until")
        valid_until = (
            _NO_EXPIRY_SENTINEL
            if valid_until_raw is None
            else _aware_datetime(valid_until_raw, name="relationship end")
        )
        relationship_id = _required_string(item.get("relationship_id"), name="relationship id")
        revision = max(1, int(updated_at.timestamp() * 1_000_000))
        relationships.append(
            RelationshipEvidence(
                relationship_id=relationship_id,
                snapshot_id=f"{relationship_id}:v{revision}",
                revision=revision,
                relation_type=cast(
                    RelationTypeValue,
                    _required_string(item.get("relation_type"), name="relationship type"),
                ),
                status=cast(
                    RelationshipStatusValue,
                    _required_string(item.get("status"), name="relationship status"),
                ),
                source_person_id=_required_string(
                    item.get("source_person_id"), name="relationship source"
                ),
                target_person_id=_required_string(
                    item.get("target_person_id"), name="relationship target"
                ),
                binding_id=binding_id,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        )
    return _LockedBinding(
        evidence=_LockedBindingEvidence(
            binding_id=binding_id,
            version=version,
            device_id=device_id,
            status="active",
            declared_mode=declared_mode,
            valid_from=valid_from,
            valid_until=valid_until,
            canonical_hash=_digest(evidence_payload),
        ),
        roles=tuple(roles),
        subjects=tuple(subjects),
        relationships=tuple(relationships),
        account_owner_person_id=_required_string(
            raw.get("account_owner_person_id"), name="account owner"
        ),
        policy_bundle_version=_required_string(
            raw.get("policy_bundle_version"), name="policy bundle version"
        ),
        persona_assignment_id=_required_string(
            raw.get("persona_assignment_id"), name="persona assignment"
        ),
    )


class _PostgresIdentityAuthority:
    async def lock_binding(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        expected_binding_version: int,
        now: datetime,
    ) -> _LockedBinding:
        try:
            raw = await connection.fetchval(
                "SELECT action_identity_lock_binding($1, $2, $3, $4)",
                actor_id,
                device_id,
                expected_binding_version,
                now,
            )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionDenied(str(exc)) from exc
        if raw is None:
            raise PersistentSessionDenied("active actor/device binding is unavailable")
        binding = _parse_locked_binding(raw)
        if (
            binding.evidence.device_id != device_id
            or binding.evidence.version != expected_binding_version
            or not any(item.person_id == actor_id for item in binding.roles)
        ):
            raise PersistentSessionDenied("binding authority fence mismatch")
        return binding

    async def can_switch_subject(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        binding_version: int,
        subject_id: str,
    ) -> bool:
        try:
            allowed = await connection.fetchval(
                "SELECT action_identity_can_switch_subject($1, $2, $3, $4)",
                actor_id,
                device_id,
                binding_version,
                subject_id,
            )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        return allowed is True

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> BindingEvidencePort:
        binding = await self.lock_binding(
            connection,
            actor_id=request.context.actor_id,
            device_id=request.context.device_id,
            expected_binding_version=request.context.binding_version,
            now=request.now,
        )
        if (
            binding.evidence.binding_id != receipt.binding_id
            or binding.evidence.canonical_hash != receipt.binding_canonical_hash
        ):
            raise ActionAuthorizationError("current binding evidence mismatch")
        return binding.evidence


class _PostgresIdentityRelationshipAuthority:
    def __init__(self, identity: _PostgresIdentityAuthority) -> None:
        self._identity = identity

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[RelationshipEvidencePort, ...]:
        binding = await self._identity.lock_binding(
            connection,
            actor_id=request.context.actor_id,
            device_id=request.context.device_id,
            expected_binding_version=request.context.binding_version,
            now=request.now,
        )
        expected = set(
            zip(
                receipt.relationship_snapshot_ids,
                receipt.relationship_snapshot_revisions,
                strict=True,
            )
        )
        current = tuple(
            item for item in binding.relationships if (item.snapshot_id, item.revision) in expected
        )
        if {(item.snapshot_id, item.revision) for item in current} != expected:
            raise ActionAuthorizationError("current relationship evidence mismatch")
        return current


@dataclass(frozen=True, slots=True)
class _DeviceTrustSnapshot:
    available: bool
    trust: str
    reason_code: str


class _PostgresDeviceAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        now: datetime,
    ) -> _DeviceTrustSnapshot:
        try:
            raw = await connection.fetchval(
                "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
                actor_id,
                device_id,
                binding_id,
                binding_version,
                now,
            )
        except asyncpg.UndefinedFunctionError:
            return _DeviceTrustSnapshot(
                available=False,
                trust="untrusted",
                reason_code="device_authority_unavailable",
            )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        payload = _decode_json_object(raw, name="Device trust authority")
        available = payload.get("available")
        trust = payload.get("device_trust")
        if not isinstance(available, bool) or trust not in {
            "trusted",
            "verified",
            "offline",
            "untrusted",
            "revoked",
        }:
            raise PersistentSessionUnavailable("Device trust authority is invalid")
        reason = payload.get("reason_code")
        return _DeviceTrustSnapshot(
            available=available,
            trust=str(trust),
            reason_code=str(reason) if isinstance(reason, str) else "unknown",
        )


class _PostgresPrincipalAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> str:
        del receipt
        actor = await connection.fetchval(
            "SELECT NULLIF(current_setting('app.authenticated_actor', true), '')"
        )
        if not isinstance(actor, str) or actor != request.context.actor_id:
            raise ActionAuthorizationError("authenticated principal is unavailable")
        return actor


class _PostgresSessionActionAuthority:
    def __init__(self, device: _PostgresDeviceAuthority) -> None:
        self._device = device

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyActionResourceFence:
        expected = effective_action_resource_fence(request.context)
        if expected != receipt.action_resource_fence:
            raise ActionAuthorizationError("Session action fence mismatch")
        trust = await self._device.lock_current(
            connection,
            actor_id=request.context.actor_id,
            device_id=request.context.device_id,
            binding_id=request.context.binding_id,
            binding_version=request.context.binding_version,
            now=request.now,
        )
        if trust.trust != request.context.device_trust:
            raise ActionAuthorizationError("current Device trust fence mismatch")
        valid = await connection.fetchval(
            """
            SELECT session_runtime_assert_action_context(
                $1, $2, $3, $4, $5, $6, $7
            )
            """,
            receipt.session_id,
            receipt.runtime_profile_id,
            receipt.actor_id,
            receipt.device_id,
            receipt.binding_id,
            receipt.binding_version,
            receipt.session_epoch,
        )
        if valid is not True:
            raise ActionAuthorizationError("Session action authority is unavailable")
        return receipt.action_resource_fence


class _ActionExecutorReceiptRepository(ConnectionBoundPolicyReceiptRepository):
    """Append receipts through the Policy SECURITY DEFINER bridge only."""

    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        if not connection.is_in_transaction():
            raise RuntimeError("receipt batch requires caller-owned transaction")
        if len({item.receipt_id for item in receipts}) != len(receipts):
            raise PolicyReceiptConflictError("receipt batch contains duplicate ids")
        for receipt in sorted(receipts, key=lambda item: item.receipt_id):
            raw = await connection.fetchval(
                "SELECT action_policy_insert_receipt($1::jsonb)",
                _canonical_json(receipt.model_dump(mode="json")),
            )
            persisted = PolicyReceiptV2.model_validate(
                _decode_json_object(raw, name="persisted Policy receipt")
            )
            if persisted != receipt:
                raise PolicyReceiptConflictError(
                    "policy receipt id is immutable and content differs"
                )


def _persona_snapshot(assignment_id: str) -> tuple[str, int]:
    persona_id, separator, raw_version = assignment_id.rpartition(":v")
    if not separator or not persona_id or not raw_version.isdigit():
        raise PersistentSessionUnavailable(
            "persona assignment lacks a canonical persona/version snapshot"
        )
    version = int(raw_version)
    if version < 1:
        raise PersistentSessionUnavailable("persona version is invalid")
    return persona_id, version


def _obligation_union(
    receipts: tuple[PolicyReceiptV2, ...],
) -> tuple[PolicyObligationSpec, ...]:
    by_code: dict[str, PolicyObligationSpec] = {}
    for receipt in receipts:
        for obligation in receipt.obligations:
            encoded = _canonical_json(obligation.model_dump(mode="json"))
            previous = by_code.get(obligation.code.value)
            if (
                previous is not None
                and _canonical_json(previous.model_dump(mode="json")) != encoded
            ):
                raise PersistentSessionUnavailable(
                    "Policy returned conflicting obligation parameters"
                )
            by_code[obligation.code.value] = obligation
    return tuple(by_code[key] for key in sorted(by_code))


@dataclass(frozen=True, slots=True)
class _IssuedProfile:
    profile: RuntimeProfileSignedV2
    event: SessionEvent
    allowed: tuple[tuple[PolicyContext, PolicyReceiptV2, object | None], ...]
    denied: tuple[PolicyReceiptV2, ...]

    @property
    def allowed_receipts(self) -> tuple[PolicyReceiptV2, ...]:
        return tuple(item[1] for item in self.allowed)

    def requests(self, *, now: datetime) -> tuple[ActionExecutionRequest, ...]:
        return tuple(
            ActionExecutionRequest(
                receipt_id=receipt.receipt_id,
                context=context,
                now=now,
                consent_authority_proof=consent_authority_proof,
            )
            for context, receipt, consent_authority_proof in self.allowed
        )


class PostgresSessionRuntimeService:
    """Create signed V2 profiles and their complete authority history."""

    def __init__(
        self,
        *,
        store: PostgresSessionRuntimeStore,
        signing_key: bytes,
        policy: PolicyEngine,
        identity: _PostgresIdentityAuthority,
        device: _PostgresDeviceAuthority,
        receipt_repository: _ActionExecutorReceiptRepository,
        profile_ttl: timedelta = _DEFAULT_PROFILE_TTL,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("production signing_key must contain at least 32 bytes")
        if profile_ttl <= timedelta(0):
            raise ValueError("profile_ttl must be positive")
        self._store = store
        self._signing_key = signing_key
        self._policy = policy
        self._identity = identity
        self._device = device
        self._receipt_repository = receipt_repository
        self._consent = PostgresCurrentConsentAuthorityAdapter()
        self._profile_ttl = profile_ttl
        self._subject_resolver = SubjectResolver()
        self._mode_resolver = ServiceModeResolver()
        self._batch = build_session_batch_service(
            ProductionAuthorityAdapters(
                principal=_PostgresPrincipalAuthority(),
                consent=self._consent,
                relationship=_PostgresIdentityRelationshipAuthority(identity),
                binding=identity,
                action=_PostgresSessionActionAuthority(device),
            ),
            profile_authority=store,
            repository=receipt_repository,
        )

    async def binding_snapshot(
        self,
        *,
        actor_id: str,
        device_id: str,
        expected_binding_version: int,
        now: datetime,
    ) -> BindingSnapshot:
        try:
            async with self._store.action_transaction(
                actor_id=actor_id,
                device_id=device_id,
            ) as connection:
                binding = await self._identity.lock_binding(
                    connection,
                    actor_id=actor_id,
                    device_id=device_id,
                    expected_binding_version=expected_binding_version,
                    now=now,
                )
                return binding.snapshot
        except PersistentSessionDenied:
            raise
        except (asyncpg.PostgresError, SessionRuntimeAuthorityUnavailable) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc

    async def start(
        self,
        command: StartPersistentSessionCommand,
        *,
        before_commit: Callable[[RuntimeProfileSignedV2], Awaitable[None]] | None = None,
    ) -> RuntimeProfileSignedV2:
        requested = tuple(
            capability
            for capability in command.requested_capabilities
            if capability in PROFILE_ISSUE_SESSION_CAPABILITIES
        )
        if any(
            capability not in PROFILE_ISSUE_SESSION_CAPABILITIES
            and capability not in PROFILE_ISSUE_DEFERRED_CAPABILITIES
            for capability in command.requested_capabilities
        ):
            raise PersistentSessionDenied("unknown profile capability requested")
        if not requested:
            raise PersistentSessionDenied(
                "resource-scoped capabilities cannot start a Session profile"
            )
        try:
            async with self._store.action_transaction(
                actor_id=command.actor_id,
                device_id=command.device_id,
            ) as connection:
                binding = await self._identity.lock_binding(
                    connection,
                    actor_id=command.actor_id,
                    device_id=command.device_id,
                    expected_binding_version=command.expected_binding_version,
                    now=command.now,
                )
                trust = await self._device.lock_current(
                    connection,
                    actor_id=command.actor_id,
                    device_id=command.device_id,
                    binding_id=binding.evidence.binding_id,
                    binding_version=binding.evidence.version,
                    now=command.now,
                )
                if trust.trust == "revoked":
                    raise PersistentSessionDenied(trust.reason_code)
                profile = await self._start_locked(
                    connection,
                    command=command,
                    binding=binding,
                    requested=requested,
                    device_trust=trust.trust,
                )
                if before_commit is not None:
                    await before_commit(profile)
                return profile
        except PersistentSessionDenied:
            raise
        except SessionRuntimeConflict:
            raise
        except (
            ActionAuthorizationError,
            SessionRuntimeAuthorityUnavailable,
            asyncpg.PostgresError,
        ) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc

    async def _start_locked(
        self,
        connection: asyncpg.Connection,
        *,
        command: StartPersistentSessionCommand,
        binding: _LockedBinding,
        requested: tuple[CapabilityValue, ...],
        device_trust: str,
    ) -> RuntimeProfileSignedV2:
        resolution = self._subject_resolver.resolve(
            ResolveSubjectCommand(
                device_id=command.device_id,
                candidates=command.candidates,
                multiple_speakers=command.multiple_speakers,
                offline=command.offline,
            ),
            binding=binding.snapshot,
        )
        await self._store.set_action_subject(
            connection,
            resolution.active_subject_id,
        )
        issue = await self._build_issue(
            connection,
            binding=binding,
            actor_id=command.actor_id,
            device_id=command.device_id,
            session_id=command.session_id,
            session_epoch=1,
            profile_revision=1,
            resolution=resolution,
            requested=requested,
            device_trust=device_trust,
            now=command.now,
            idempotency_seed=command.idempotency_key,
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
            event_type="subject_resolved",
            event_payload={},
        )
        context = SessionRuntimeContext.from_profile(
            issue.profile,
            profile_revision=1,
        )
        request_hash = _digest(
            {
                "session_id": command.session_id,
                "actor_id": command.actor_id,
                "device_id": command.device_id,
                "binding_version": command.expected_binding_version,
                "requested_capabilities": list(command.requested_capabilities),
                "multiple_speakers": command.multiple_speakers,
                "offline": command.offline,
            }
        )
        replay = await self._store.prepare_initial(
            connection,
            context=context,
            request_hash=request_hash,
            idempotency_key=command.idempotency_key,
        )
        if replay is not None:
            return replay

        async def persist(
            current_connection: asyncpg.Connection,
            locked_receipts: tuple[PolicyReceiptV2, ...],
        ) -> RuntimeProfileSignedV2:
            self._require_locked_receipts(issue, locked_receipts)
            return await self._store.commit_initial(
                current_connection,
                context=context,
                profile=issue.profile,
                event=issue.event,
                idempotency_key=command.idempotency_key,
            )

        return await self._persist_issue(
            connection,
            issue=issue,
            now=command.now,
            write_callback=persist,
        )

    async def _discover_current_consent(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        subject_id: str | None,
        resource_owner_id: str | None,
        device_id: str,
        binding_id: str,
        binding_version: int,
        capability: CapabilityValue,
        purpose: PurposeValue,
        now: datetime,
    ) -> ConsentDiscovery:
        """Return one current Consent view without making it a hard dependency.

        Deployments upgrade Session and Consent additively.  While the narrow
        discovery port is absent, Policy receives empty evidence and therefore
        denies every consent-gated capability; non-consent capabilities remain
        available.  Once installed, discovery and receipt persistence share
        the caller-owned action transaction and its proof is consumed by the
        batch authorizer.
        """
        if subject_id is None:
            return ConsentDiscovery((), ())
        available = await connection.fetchval(
            "SELECT to_regprocedure($1) IS NOT NULL",
            "public.consent_discover_action_fence("
            "text,text,text,text,text,integer,text,text,timestamptz)",
        )
        if available is not True:
            return ConsentDiscovery((), ())
        discovery = await self._consent.discover_current(
            connection,
            actor_id=actor_id,
            subject_id=subject_id,
            resource_owner_id=resource_owner_id,
            device_id=device_id,
            binding_id=binding_id,
            binding_version=binding_version,
            capability=capability,
            purpose=purpose,
            now=now,
        )
        # A subject-global snapshot may contain grants for other
        # capabilities.  It is not evidence for this decision and must not
        # enter its receipt/action fence unless an exact capability grant was
        # discovered.
        if not discovery.consent_evidence:
            return ConsentDiscovery((), ())
        return discovery

    async def _build_issue(
        self,
        connection: asyncpg.Connection,
        *,
        binding: _LockedBinding,
        actor_id: str,
        device_id: str,
        session_id: str,
        session_epoch: int,
        profile_revision: int,
        resolution: SubjectResolution,
        requested: tuple[CapabilityValue, ...],
        device_trust: str,
        now: datetime,
        idempotency_seed: str,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        event_type: str,
        event_payload: dict[str, object],
    ) -> _IssuedProfile:
        subject = next(
            (item for item in binding.subjects if item.person_id == resolution.active_subject_id),
            None,
        )
        if resolution.active_subject_id is not None and subject is None:
            raise PersistentSessionDenied("active subject is not a binding member")
        subject_category = cast(
            SubjectCategoryValue,
            subject.subject_category if subject is not None else "unknown",
        )
        age_band = cast(
            AgeBandValue,
            subject.age_band if subject is not None else "unknown",
        )
        service_mode = self._mode_resolver.resolve(
            declared_mode=binding.evidence.declared_mode,
            resolution=resolution,
            subject_category=subject_category,
            age_band=age_band,
        )
        profile_id = f"rp-{uuid.uuid4()}"
        contexts_with_proof: list[tuple[PolicyContext, object | None]] = []
        for capability in requested:
            purpose = canonical_runtime_decision_purpose(capability)
            discovery = await self._discover_current_consent(
                connection,
                actor_id=actor_id,
                subject_id=resolution.active_subject_id,
                resource_owner_id=resolution.active_subject_id,
                device_id=device_id,
                binding_id=binding.evidence.binding_id,
                binding_version=binding.evidence.version,
                capability=capability,
                purpose=purpose,
                now=now,
            )
            context = PolicyContext(
                actor_id=actor_id,
                subject_id=resolution.active_subject_id,
                resource_owner_id=resolution.active_subject_id,
                device_id=device_id,
                capability=capability,
                purpose=purpose,
                declared_device_mode=binding.evidence.declared_mode,
                current_session_mode=service_mode,
                subject_category=subject_category,
                age_band=age_band,
                speaker_state=resolution.speaker_state,
                speaker_confidence=resolution.speaker_confidence,
                device_trust=device_trust,
                safety_state="normal",
                jurisdiction="CN",
                data_classification="private",
                binding_id=binding.evidence.binding_id,
                binding_version=binding.evidence.version,
                session_id=session_id,
                session_epoch=session_epoch,
                runtime_profile_id=profile_id,
                subject_revision=subject.revision if subject is not None else 0,
                evaluated_at=now,
                idempotency_key=_digest((actor_id, idempotency_seed, session_epoch, capability)),
                binding_evidence=binding.evidence,
                relationship_evidence=binding.relationships,
                consent_evidence=discovery.consent_evidence,
                consent_snapshot_evidence=discovery.consent_snapshot_evidence,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
            )
            contexts_with_proof.append((context, discovery.authority_proof))
        contexts = tuple(item[0] for item in contexts_with_proof)
        decisions = tuple(self._policy.decide(context) for context in contexts)
        receipts = tuple(
            self._policy.receipt_for(context, decision)
            for context, decision in zip(contexts, decisions, strict=True)
        )
        allowed = tuple(
            (context, receipt, proof)
            for (context, proof), receipt in zip(
                contexts_with_proof, receipts, strict=True
            )
            if receipt.effect.value in {"allow", "allow_with_obligations"}
        )
        denied = tuple(
            receipt
            for receipt in receipts
            if receipt.effect.value not in {"allow", "allow_with_obligations"}
        )
        allowed_receipts = tuple(item[1] for item in allowed)
        persona_id, persona_version = _persona_snapshot(binding.persona_assignment_id)
        unsigned = RuntimeProfileV2.model_validate(
            {
                "signature_schema": "runtime-profile-v2",
                "runtime_profile_id": profile_id,
                "device_id": device_id,
                "session_id": session_id,
                "actor_id": actor_id,
                "binding_id": binding.evidence.binding_id,
                "binding_version": binding.evidence.version,
                "active_subject_id": resolution.active_subject_id,
                "subject_revision": subject.revision if subject is not None else 0,
                "subject_category": subject_category,
                "age_band": age_band,
                "speaker_state": resolution.speaker_state,
                "speaker_confidence": resolution.speaker_confidence,
                "service_mode": service_mode,
                "persona_assignment_id": binding.persona_assignment_id,
                "persona": {
                    "persona_id": persona_id,
                    "version": persona_version,
                    "relationship_stage": "new",
                },
                "policy_bundle_version": binding.policy_bundle_version,
                "capabilities": [item.capability.value for item in allowed_receipts],
                "obligations": [
                    item.model_dump(mode="json") for item in _obligation_union(allowed_receipts)
                ],
                "policy_receipt_ids": [item.receipt_id for item in allowed_receipts],
                "session_epoch": session_epoch,
                "issued_at": now.isoformat(),
                "expires_at": (now + self._profile_ttl).isoformat(),
            }
        )
        payload = unsigned.model_dump(mode="json")
        signed = RuntimeProfileSignedV2.model_validate(
            {
                **payload,
                "signature": sign_runtime_profile_payload(
                    payload,
                    signing_key=self._signing_key,
                ),
            }
        )
        event = SessionEvent.model_validate(
            {
                "event_id": f"event-{uuid.uuid4()}",
                "event_type": event_type,
                "session_id": signed.session_id,
                "session_epoch": signed.session_epoch,
                "device_id": signed.device_id,
                "binding_id": signed.binding_id,
                "binding_version": signed.binding_version,
                "generation_id": generation_id,
                "turn_id": turn_id,
                "tool_epoch": tool_epoch,
                "event_sequence": profile_revision,
                "active_subject_id": signed.active_subject_id,
                "runtime_profile_id": signed.runtime_profile_id,
                "actor_id": signed.actor_id,
                "subject_revision": signed.subject_revision,
                "occurred_at": now.isoformat(),
                "payload": {
                    "reason_code": resolution.reason_code,
                    "service_mode": service_mode,
                    **event_payload,
                },
            }
        )
        return _IssuedProfile(
            profile=signed,
            event=event,
            allowed=allowed,
            denied=denied,
        )

    async def _persist_issue(
        self,
        connection: asyncpg.Connection,
        *,
        issue: _IssuedProfile,
        now: datetime,
        write_callback: Callable[
            [asyncpg.Connection, tuple[PolicyReceiptV2, ...]],
            Awaitable[RuntimeProfileSignedV2],
        ],
    ) -> RuntimeProfileSignedV2:
        if issue.denied:
            await self._receipt_repository.insert_many(connection, issue.denied)
        if not issue.allowed:
            if not issue.denied:
                raise PersistentSessionUnavailable("Policy produced no receipts")
            await self._store.lock_current(connection, issue.denied)
            return await write_callback(connection, ())
        return await self._batch.execute_profile_persist(
            connection,
            receipts=issue.allowed_receipts,
            requests=issue.requests(now=now),
            write_callback=write_callback,
        )

    @staticmethod
    def _require_locked_receipts(
        issue: _IssuedProfile,
        locked_receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        if {item.receipt_id for item in locked_receipts} != {
            item.receipt_id for item in issue.allowed_receipts
        }:
            raise PersistentSessionUnavailable(
                "locked Policy receipt batch differs from signed profile"
            )

    def _require_current_profile(
        self,
        profile: RuntimeProfileSignedV2,
        *,
        now: datetime,
    ) -> None:
        payload = profile.model_dump(mode="json", exclude={"signature"})
        expected = sign_runtime_profile_payload(payload, signing_key=self._signing_key)
        if not hmac.compare_digest(profile.signature, expected):
            raise PersistentSessionDenied("runtime profile signature is invalid")
        if now < profile.issued_at or now >= profile.expires_at:
            raise PersistentSessionDenied("runtime profile is expired")

    async def current(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]:
        try:
            async with self._store.read_transaction(actor_id=actor_id) as connection:
                profile = await self._store.current_profile(
                    connection,
                    session_id=session_id,
                )
                context = await self._store.current_context(
                    connection,
                    session_id=session_id,
                )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        if profile is None or context is None:
            raise PersistentSessionNotFound(session_id)
        if profile.runtime_profile_id != context.current_runtime_profile_id:
            raise PersistentSessionUnavailable("current profile projection is inconsistent")
        self._require_current_profile(profile, now=now)
        return profile, context

    async def switch_subject(
        self,
        command: SwitchPersistentSubjectCommand,
    ) -> RuntimeProfileSignedV2:
        requested = tuple(
            capability
            for capability in command.requested_capabilities
            if capability in PROFILE_ISSUE_SESSION_CAPABILITIES
        )
        if not requested:
            raise PersistentSessionDenied("no Session capability was requested")
        current, current_context = await self.current(
            actor_id=command.actor_id,
            session_id=command.session_id,
            now=command.now,
        )
        try:
            async with self._store.action_transaction(
                actor_id=command.actor_id,
                device_id=current.device_id,
            ) as connection:
                binding = await self._identity.lock_binding(
                    connection,
                    actor_id=command.actor_id,
                    device_id=current.device_id,
                    expected_binding_version=current.binding_version,
                    now=command.now,
                )
                authority_target = command.subject_id or command.claimed_subject_id
                if authority_target is not None and not await self._identity.can_switch_subject(
                    connection,
                    actor_id=command.actor_id,
                    device_id=current.device_id,
                    binding_version=current.binding_version,
                    subject_id=authority_target,
                ):
                    raise PersistentSessionDenied("subject switch authority denied")
                trust = await self._device.lock_current(
                    connection,
                    actor_id=command.actor_id,
                    device_id=current.device_id,
                    binding_id=current.binding_id,
                    binding_version=current.binding_version,
                    now=command.now,
                )
                if trust.trust == "revoked":
                    raise PersistentSessionDenied(trust.reason_code)
                if command.subject_id is not None:
                    if not trust.available:
                        raise PersistentSessionUnavailable(
                            "confirmed subject requires Device trust authority"
                        )
                    resolution = self._subject_resolver.resolve(
                        ResolveSubjectCommand(
                            device_id=current.device_id,
                            app_claimed_subject_id=command.subject_id,
                        ),
                        binding=binding.snapshot,
                    )
                else:
                    resolution = SubjectResolution(
                        active_subject_id=None,
                        speaker_state="unconfirmed",
                        speaker_confidence=None,
                        service_mode="unknown_safe",
                        reason_code="subject_change_requires_confirmation",
                        requires_confirmation=True,
                    )
                await self._store.set_action_subject(
                    connection,
                    resolution.active_subject_id,
                )
                issue = await self._build_issue(
                    connection,
                    binding=binding,
                    actor_id=command.actor_id,
                    device_id=current.device_id,
                    session_id=current.session_id,
                    session_epoch=current.session_epoch + 1,
                    profile_revision=current_context.profile_revision + 1,
                    resolution=resolution,
                    requested=requested,
                    device_trust=trust.trust,
                    now=command.now,
                    idempotency_seed=(
                        f"switch:{current.runtime_profile_id}:"
                        f"{command.subject_id or command.claimed_subject_id or 'unknown'}"
                    ),
                    generation_id=current_context.generation_id + 1,
                    turn_id=current_context.turn_id + 1,
                    tool_epoch=current_context.tool_epoch + 1,
                    event_type=(
                        "subject_switched" if command.subject_id is not None else "epoch_bumped"
                    ),
                    event_payload={
                        "claimed_subject_id": command.claimed_subject_id,
                        "invalidated_fences": [
                            "generation",
                            "tool",
                            "effect",
                            "tts",
                            "memory",
                            "ui",
                        ],
                    },
                )
                await self._store.prepare_rotation(
                    connection,
                    expected=current_context,
                    profile=issue.profile,
                    event=issue.event,
                )

                async def persist_rotation(
                    current_connection: asyncpg.Connection,
                    locked_receipts: tuple[PolicyReceiptV2, ...],
                ) -> RuntimeProfileSignedV2:
                    self._require_locked_receipts(issue, locked_receipts)
                    return await self._store.commit_rotation(
                        current_connection,
                        profile_revision=current_context.profile_revision + 1,
                        profile=issue.profile,
                        event=issue.event,
                    )

                return await self._persist_issue(
                    connection,
                    issue=issue,
                    now=command.now,
                    write_callback=persist_rotation,
                )
        except (PersistentSessionDenied, PersistentSessionNotFound):
            raise
        except SessionRuntimeConflict:
            raise
        except (
            ActionAuthorizationError,
            SessionRuntimeAuthorityUnavailable,
            asyncpg.PostgresError,
        ) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc

    async def fail_session(
        self,
        *,
        actor_id: str,
        session_id: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        profile, context = await self.current(
            actor_id=actor_id,
            session_id=session_id,
            now=now,
        )
        if profile.actor_id != actor_id:
            raise PersistentSessionDenied("only the profile actor may fail Session start")
        event = SessionEvent.model_validate(
            {
                "event_id": f"event-{uuid.uuid4()}",
                "event_type": "session_failed",
                "session_id": session_id,
                "session_epoch": context.session_epoch + 1,
                "device_id": profile.device_id,
                "binding_id": profile.binding_id,
                "binding_version": profile.binding_version,
                "generation_id": context.generation_id + 1,
                "turn_id": context.turn_id + 1,
                "tool_epoch": context.tool_epoch + 1,
                "event_sequence": context.profile_revision + 1,
                "active_subject_id": None,
                "runtime_profile_id": profile.runtime_profile_id,
                "actor_id": actor_id,
                "subject_revision": 0,
                "occurred_at": now.isoformat(),
                "payload": {
                    "reason_code": reason_code,
                    "invalidated_fences": [
                        "generation",
                        "tool",
                        "effect",
                        "tts",
                        "memory",
                        "ui",
                    ],
                },
            }
        )
        try:
            async with self._store.action_transaction(
                actor_id=actor_id,
                device_id=profile.device_id,
                subject_id=context.active_subject_id,
            ) as connection:
                await self._store.fail_session(
                    connection,
                    expected=context,
                    event=event,
                )
        except (SessionRuntimeConflict, PersistentSessionDenied):
            raise
        except (SessionRuntimeAuthorityUnavailable, asyncpg.PostgresError) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc

    @staticmethod
    def _action_fence_kind(
        current: SessionRuntimeContext,
        *,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
    ) -> str:
        requested = (generation_id, turn_id, tool_epoch)
        if requested == (
            current.generation_id + 1,
            current.turn_id + 1,
            current.tool_epoch,
        ):
            return "turn"
        if requested == (
            current.generation_id + 1,
            current.turn_id,
            current.tool_epoch,
        ):
            return "interrupt"
        if requested == (
            current.generation_id + 1,
            current.turn_id,
            current.tool_epoch + 1,
        ):
            return "tool"
        raise SessionRuntimeConflict(
            "action fence must be one legal turn, interrupt, or tool transition"
        )

    @staticmethod
    def _require_action_profile(
        profile: RuntimeProfileSignedV2,
        context: SessionRuntimeContext,
        *,
        actor_id: str,
        session_id: str,
        runtime_profile_id: str,
        session_epoch: int,
    ) -> None:
        if profile.actor_id != actor_id or context.actor_id != actor_id:
            raise PersistentSessionDenied("runtime profile actor mismatch")
        if profile.session_id != session_id or context.session_id != session_id:
            raise PersistentSessionDenied("runtime profile Session mismatch")
        if (
            profile.runtime_profile_id != runtime_profile_id
            or context.current_runtime_profile_id != runtime_profile_id
        ):
            raise PersistentSessionDenied("runtime profile is stale")
        if session_epoch != context.session_epoch:
            if session_epoch < context.session_epoch:
                raise SessionRuntimeConflict("Session epoch is stale")
            raise PersistentSessionDenied("Session epoch is forged")
        projection = (
            profile.device_id,
            profile.binding_id,
            profile.binding_version,
            profile.active_subject_id,
            profile.subject_revision,
            profile.session_epoch,
        )
        authority = (
            context.device_id,
            context.binding_id,
            context.binding_version,
            context.active_subject_id,
            context.subject_revision,
            context.session_epoch,
        )
        if projection != authority:
            raise PersistentSessionUnavailable("runtime profile projection is inconsistent")

    @staticmethod
    def _require_action_binding(
        profile: RuntimeProfileSignedV2,
        binding: _LockedBinding,
    ) -> None:
        if (
            binding.evidence.binding_id != profile.binding_id
            or binding.evidence.device_id != profile.device_id
            or binding.evidence.version != profile.binding_version
        ):
            raise PersistentSessionDenied("binding authority fence mismatch")
        if profile.active_subject_id is None:
            if profile.subject_revision != 0:
                raise PersistentSessionUnavailable("unconfirmed profile carries a subject revision")
            return
        subject = next(
            (item for item in binding.subjects if item.person_id == profile.active_subject_id),
            None,
        )
        if subject is None:
            raise PersistentSessionDenied("active subject is outside the binding")
        if subject.revision != profile.subject_revision:
            raise PersistentSessionDenied("subject revision is stale")
        if (
            subject.subject_category != profile.subject_category.value
            or subject.age_band != profile.age_band.value
        ):
            raise PersistentSessionDenied("subject classification is stale")

    @staticmethod
    def _action_idempotency_key(
        profile: RuntimeProfileSignedV2,
        *,
        actor_id: str,
        capability: CapabilityValue,
        purpose: str,
        session_epoch: int,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        data_classification: str,
        safety_state: str,
    ) -> str:
        return _digest(
            {
                "schema": "session-action-v1",
                "actor_id": actor_id,
                "subject_id": profile.active_subject_id,
                "resource_owner_id": profile.active_subject_id,
                "device_id": profile.device_id,
                "binding_id": profile.binding_id,
                "binding_version": profile.binding_version,
                "session_id": profile.session_id,
                "runtime_profile_id": profile.runtime_profile_id,
                "capability": capability,
                "purpose": purpose,
                "session_epoch": session_epoch,
                "generation_id": generation_id,
                "turn_id": turn_id,
                "tool_epoch": tool_epoch,
                "data_classification": data_classification,
                "safety_state": safety_state,
            }
        )

    def _action_context(
        self,
        *,
        profile: RuntimeProfileSignedV2,
        binding: _LockedBinding,
        trust: _DeviceTrustSnapshot,
        actor_id: str,
        capability: CapabilityValue,
        session_epoch: int,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        data_classification: str,
        safety_state: str,
        now: datetime,
        consent_evidence: tuple[ConsentEvidencePort, ...] = (),
        consent_snapshot_evidence: tuple[ConsentSnapshotEvidencePort, ...] = (),
    ) -> PolicyContext:
        purpose = canonical_runtime_decision_purpose(capability)
        return PolicyContext(
            actor_id=actor_id,
            subject_id=profile.active_subject_id,
            resource_owner_id=profile.active_subject_id,
            device_id=profile.device_id,
            capability=capability,
            purpose=purpose,
            declared_device_mode=binding.evidence.declared_mode,
            current_session_mode=profile.service_mode.value,
            subject_category=profile.subject_category.value,
            age_band=profile.age_band.value,
            speaker_state=profile.speaker_state.value,
            speaker_confidence=profile.speaker_confidence,
            device_trust=trust.trust,
            safety_state=safety_state,
            jurisdiction="CN",
            data_classification=data_classification,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            session_id=profile.session_id,
            session_epoch=session_epoch,
            runtime_profile_id=profile.runtime_profile_id,
            subject_revision=profile.subject_revision,
            evaluated_at=now,
            idempotency_key=self._action_idempotency_key(
                profile,
                actor_id=actor_id,
                capability=capability,
                purpose=purpose,
                session_epoch=session_epoch,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
                data_classification=data_classification,
                safety_state=safety_state,
            ),
            consent_evidence=consent_evidence,
            consent_snapshot_evidence=consent_snapshot_evidence,
            relationship_evidence=binding.relationships,
            binding_evidence=binding.evidence,
            generation_id=generation_id,
            turn_id=turn_id,
            tool_epoch=tool_epoch,
        )

    def _require_replay_receipt(
        self,
        receipt: PolicyReceiptV2,
        *,
        receipt_id: str,
        context: PolicyContext,
        binding: _LockedBinding,
        now: datetime,
    ) -> None:
        literal_pairs = (
            (receipt.receipt_id, receipt_id),
            (receipt.actor_id, context.actor_id),
            (receipt.subject_id, context.subject_id),
            (receipt.resource_owner_id, context.resource_owner_id),
            (receipt.device_id, context.device_id),
            (receipt.capability.value, context.capability),
            (receipt.purpose.value, context.purpose),
            (receipt.binding_id, context.binding_id),
            (receipt.binding_version, context.binding_version),
            (receipt.session_id, context.session_id),
            (receipt.session_epoch, context.session_epoch),
            (receipt.runtime_profile_id, context.runtime_profile_id),
            (receipt.subject_revision, context.subject_revision),
            (receipt.device_trust.value, context.device_trust),
            (receipt.data_classification.value, context.data_classification),
            (receipt.safety_state.value, context.safety_state),
            (receipt.jurisdiction, context.jurisdiction),
            (receipt.policy_version, self._policy.policy_version),
            (
                receipt.binding_canonical_hash,
                binding.evidence.canonical_hash,
            ),
        )
        if any(left != right for left, right in literal_pairs):
            raise PersistentSessionDenied("stored action receipt identity mismatch")
        if not receipt.exact_fence:
            raise PersistentSessionDenied("stored action receipt is not exact-fenced")
        fence = receipt.action_resource_fence
        expected = effective_action_resource_fence(context)
        fence_pairs = (
            (fence.capability.value, context.capability),
            (fence.purpose.value, context.purpose),
            (fence.action_resource_id, expected.action_resource_id),
            (fence.action_revision, expected.action_revision),
            (fence.generation_id, context.generation_id),
            (fence.turn_id, context.turn_id),
            (fence.tool_epoch, context.tool_epoch),
            (fence.family_space_id, expected.family_space_id),
            (
                fence.family_owner_subject_id,
                expected.family_owner_subject_id,
            ),
            (fence.proposal_id, expected.proposal_id),
            (fence.proposal_revision, expected.proposal_revision),
            (fence.voter_subject_id, expected.voter_subject_id),
            (fence.approval_decision, expected.approval_decision),
            (
                fence.required_approval_subject_ids,
                expected.required_approval_subject_ids,
            ),
            (fence.approval_snapshots, expected.approval_snapshots),
            (fence.capture_evidence_ids, expected.capture_evidence_ids),
            (fence.capture_evidence_hash, expected.capture_evidence_hash),
            (fence.membership_snapshot_id, expected.membership_snapshot_id),
            (
                fence.membership_snapshot_revision,
                expected.membership_snapshot_revision,
            ),
            (
                fence.membership_snapshot_hash,
                expected.membership_snapshot_hash,
            ),
        )
        if any(left != right for left, right in fence_pairs):
            raise PersistentSessionDenied("stored action receipt fence mismatch")
        if receipt.action_fence_hash != fence.canonical_hash:
            raise PersistentSessionDenied("stored action receipt hash mismatch")
        if (
            receipt.created_at > now
            or now >= receipt.expires_at
            or fence.issued_at > now
            or now >= fence.valid_until
        ):
            raise PersistentSessionDenied("stored action receipt is expired")

    async def _lock_terminal_action_receipt(
        self,
        connection: asyncpg.Connection,
        *,
        receipt: PolicyReceiptV2,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
    ) -> PolicyReceiptV2:
        locked = await self._store.lock_receipt_authority(
            connection,
            receipt=receipt,
            generation_id=generation_id,
            turn_id=turn_id,
            tool_epoch=tool_epoch,
        )
        if locked is None:
            raise SessionRuntimeConflict(
                "action receipt no longer matches the current Session fence"
            )
        if locked != receipt:
            raise PersistentSessionUnavailable(
                "locked action receipt differs from immutable receipt"
            )
        return locked

    async def _authorize_action_locked(
        self,
        connection: asyncpg.Connection,
        *,
        profile: RuntimeProfileSignedV2,
        current_context: SessionRuntimeContext,
        binding: _LockedBinding,
        trust: _DeviceTrustSnapshot,
        actor_id: str,
        capability: CapabilityValue,
        session_epoch: int,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        data_classification: str,
        safety_state: str,
        now: datetime,
    ) -> PolicyReceiptV2:
        replay_context = self._action_context(
            profile=profile,
            binding=binding,
            trust=trust,
            actor_id=actor_id,
            capability=capability,
            session_epoch=session_epoch,
            generation_id=generation_id,
            turn_id=turn_id,
            tool_epoch=tool_epoch,
            data_classification=data_classification,
            safety_state=safety_state,
            now=now,
        )
        receipt_id = self._policy.receipt_id_for(replay_context)
        existing = await self._receipt_repository.get_for_update(
            connection,
            receipt_id,
        )
        if existing is not None:
            self._require_replay_receipt(
                existing,
                receipt_id=receipt_id,
                context=replay_context,
                binding=binding,
                now=now,
            )
            if existing.effect.value not in {"allow", "allow_with_obligations"}:
                return await self._lock_terminal_action_receipt(
                    connection,
                    receipt=existing,
                    generation_id=generation_id,
                    turn_id=turn_id,
                    tool_epoch=tool_epoch,
                )
            discovery = await self._consent.discover_current(
                connection,
                actor_id=actor_id,
                subject_id=profile.active_subject_id,
                resource_owner_id=profile.active_subject_id,
                device_id=profile.device_id,
                binding_id=profile.binding_id,
                binding_version=profile.binding_version,
                capability=capability,
                purpose=canonical_runtime_decision_purpose(capability),
                now=now,
            )
            current_replay_context = self._action_context(
                profile=profile,
                binding=binding,
                trust=trust,
                actor_id=actor_id,
                capability=capability,
                session_epoch=session_epoch,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
                data_classification=data_classification,
                safety_state=safety_state,
                now=now,
                consent_evidence=tuple(discovery.consent_evidence),
                consent_snapshot_evidence=tuple(
                    discovery.consent_snapshot_evidence
                ),
            )
            if self._policy.receipt_id_for(current_replay_context) != receipt_id:
                raise PersistentSessionDenied(
                    "stored action receipt identity changed during replay"
                )
            current_decision = self._policy.decide(current_replay_context)
            if current_decision.effect.value not in {
                "allow",
                "allow_with_obligations",
            }:
                raise PersistentSessionDenied(
                    "stored action receipt is no longer authorized by current "
                    f"Consent: {current_decision.reason_code}"
                )
            receipted_context = replace(
                current_replay_context,
                evaluated_at=existing.created_at,
            )
            if not exact_evidence_fence_valid(
                existing,
                context=receipted_context,
                now=now,
            ):
                raise PersistentSessionDenied(
                    "stored action receipt no longer matches current authority"
                )

            async def replayed_receipt(
                current_connection: asyncpg.Connection,
                locked_receipts: tuple[PolicyReceiptV2, ...],
            ) -> PolicyReceiptV2:
                if locked_receipts != (existing,):
                    raise PersistentSessionUnavailable(
                        "replayed Policy action receipt lock mismatch"
                    )
                return await self._lock_terminal_action_receipt(
                    current_connection,
                    receipt=existing,
                    generation_id=generation_id,
                    turn_id=turn_id,
                    tool_epoch=tool_epoch,
                )

            return await self._batch.execute_profile_persist(
                connection,
                receipts=(existing,),
                requests=(
                    ActionExecutionRequest(
                        receipt_id=existing.receipt_id,
                        context=receipted_context,
                        now=now,
                        consent_authority_proof=discovery.authority_proof,
                    ),
                ),
                write_callback=replayed_receipt,
            )

        fence_kind = self._action_fence_kind(
            current_context,
            generation_id=generation_id,
            turn_id=turn_id,
            tool_epoch=tool_epoch,
        )
        advanced = await self._store.advance_action_fence(
            connection,
            context=current_context,
            fence_kind=fence_kind,
            next_generation_id=generation_id,
            next_turn_id=turn_id,
            next_tool_epoch=tool_epoch,
            occurred_at=now,
        )
        discovery = await self._consent.discover_current(
            connection,
            actor_id=actor_id,
            subject_id=profile.active_subject_id,
            resource_owner_id=profile.active_subject_id,
            device_id=profile.device_id,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            capability=capability,
            purpose=canonical_runtime_decision_purpose(capability),
            now=now,
        )
        context = self._action_context(
            profile=profile,
            binding=binding,
            trust=trust,
            actor_id=actor_id,
            capability=capability,
            session_epoch=advanced.session_epoch,
            generation_id=advanced.generation_id,
            turn_id=advanced.turn_id,
            tool_epoch=advanced.tool_epoch,
            data_classification=data_classification,
            safety_state=safety_state,
            now=now,
            consent_evidence=tuple(discovery.consent_evidence),
            consent_snapshot_evidence=tuple(discovery.consent_snapshot_evidence),
        )
        if self._policy.receipt_id_for(context) != receipt_id:
            raise PersistentSessionUnavailable(
                "action receipt identity changed after evidence discovery"
            )
        decision = self._policy.decide(context)
        receipt = self._policy.receipt_for(context, decision)
        if receipt.receipt_id != receipt_id:
            raise PersistentSessionUnavailable(
                "Policy returned an unexpected action receipt identity"
            )
        if receipt.effect.value not in {"allow", "allow_with_obligations"}:
            await self._receipt_repository.insert_many(connection, (receipt,))
            return await self._lock_terminal_action_receipt(
                connection,
                receipt=receipt,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
            )

        async def authorized_receipt(
            current_connection: asyncpg.Connection,
            locked_receipts: tuple[PolicyReceiptV2, ...],
        ) -> PolicyReceiptV2:
            if locked_receipts != (receipt,):
                raise PersistentSessionUnavailable("Policy action receipt lock mismatch")
            return await self._lock_terminal_action_receipt(
                current_connection,
                receipt=receipt,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
            )

        return await self._batch.execute_profile_persist(
            connection,
            receipts=(receipt,),
            requests=(
                ActionExecutionRequest(
                    receipt_id=receipt.receipt_id,
                    context=context,
                    now=now,
                    consent_authority_proof=discovery.authority_proof,
                ),
            ),
            write_callback=authorized_receipt,
        )

    async def authorize_action(
        self,
        *,
        actor_id: str,
        session_id: str,
        runtime_profile_id: str,
        capability: CapabilityValue,
        session_epoch: int,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        data_classification: str,
        safety_state: str,
        now: datetime,
    ) -> PolicyReceiptV2:
        """Authorize and consume one deferred action at an exact Session fence."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not actor_id.strip() or not session_id.strip() or not runtime_profile_id.strip():
            raise ValueError("action authority identifiers must not be empty")
        for value, field_name, minimum in (
            (session_epoch, "session_epoch", 1),
            (generation_id, "generation_id", 0),
            (turn_id, "turn_id", 0),
            (tool_epoch, "tool_epoch", 0),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{field_name} must be an integer >= {minimum}")
        if capability not in PROFILE_ISSUE_DEFERRED_CAPABILITIES:
            raise PersistentSessionDenied("action capability must be deferred until execution")
        if is_resource_scoped_action(capability):
            raise PersistentSessionDenied(
                "resource-scoped action requires explicit resource identity"
            )

        profile, current_context = await self.current(
            actor_id=actor_id,
            session_id=session_id,
            now=now,
        )
        self._require_action_profile(
            profile,
            current_context,
            actor_id=actor_id,
            session_id=session_id,
            runtime_profile_id=runtime_profile_id,
            session_epoch=session_epoch,
        )
        try:
            async with self._store.action_transaction(
                actor_id=actor_id,
                device_id=profile.device_id,
                subject_id=profile.active_subject_id,
            ) as connection:
                binding = await self._identity.lock_binding(
                    connection,
                    actor_id=actor_id,
                    device_id=profile.device_id,
                    expected_binding_version=profile.binding_version,
                    now=now,
                )
                self._require_action_binding(profile, binding)
                trust = await self._device.lock_current(
                    connection,
                    actor_id=actor_id,
                    device_id=profile.device_id,
                    binding_id=profile.binding_id,
                    binding_version=profile.binding_version,
                    now=now,
                )
                if trust.trust == "revoked":
                    raise PersistentSessionDenied(trust.reason_code)
                receipt = await self._authorize_action_locked(
                    connection,
                    profile=profile,
                    current_context=current_context,
                    binding=binding,
                    trust=trust,
                    actor_id=actor_id,
                    capability=capability,
                    session_epoch=session_epoch,
                    generation_id=generation_id,
                    turn_id=turn_id,
                    tool_epoch=tool_epoch,
                    data_classification=data_classification,
                    safety_state=safety_state,
                    now=now,
                )
        except (PersistentSessionDenied, SessionRuntimeConflict):
            raise
        except (
            ActionAuthorizationError,
            PolicyReceiptConflictError,
            SessionRuntimeAuthorityUnavailable,
            asyncpg.PostgresError,
            ValueError,
        ) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        if receipt.effect.value not in {"allow", "allow_with_obligations"}:
            raise PersistentSessionDenied(f"action denied by Policy: {receipt.reason_code}")
        return receipt

    async def commit_tool_effect(
        self,
        command: CommitToolEffectCommand,
    ) -> dict[str, object]:
        """Re-verify and durably record one Agent effect in one action txn."""
        profile = command.runtime_profile
        receipt = command.policy_receipt
        self._require_current_profile(profile, now=command.now)
        if (
            profile.session_id != command.session_id
            or profile.actor_id != command.actor_id
            or profile.session_epoch != command.session_epoch
            or receipt.capability.value != command.capability
            or receipt.purpose.value != command.purpose
            or receipt.action_resource_fence.action_resource_id != command.resource_id
            or command.evidence_refs != (receipt.receipt_id,)
        ):
            raise PersistentSessionDenied("tool effect authority payload mismatch")
        if command.fence_fingerprint != _tool_effect_fence_fingerprint(command):
            raise PersistentSessionDenied("tool effect fence fingerprint mismatch")
        if receipt.effect.value not in {"allow", "allow_with_obligations"}:
            raise PersistentSessionDenied("tool effect policy receipt is not allow")
        if command.capability not in PROFILE_ISSUE_DEFERRED_CAPABILITIES:
            raise PersistentSessionDenied("tool effect capability is not deferred")
        try:
            async with self._store.action_transaction(
                actor_id=command.actor_id,
                device_id=profile.device_id,
                subject_id=profile.active_subject_id,
            ) as connection:
                locked_profile, current_context = await self._store.lock_tool_effect_context(
                    connection,
                    session_id=command.session_id,
                    runtime_profile_id=profile.runtime_profile_id,
                    fence={
                        "session_id": command.session_id,
                        "session_epoch": command.session_epoch,
                        "generation_id": command.generation_id,
                        "turn_id": command.turn_id,
                        "tool_epoch": command.tool_epoch,
                    },
                    profile=profile.model_dump(mode="json"),
                )
                if locked_profile != profile:
                    raise PersistentSessionDenied(
                        "signed Runtime Profile changed during tool effect commit"
                    )
                self._require_current_profile(locked_profile, now=command.now)
                self._require_action_profile(
                    locked_profile,
                    current_context,
                    actor_id=command.actor_id,
                    session_id=command.session_id,
                    runtime_profile_id=profile.runtime_profile_id,
                    session_epoch=command.session_epoch,
                )
                binding = await self._identity.lock_binding(
                    connection,
                    actor_id=command.actor_id,
                    device_id=profile.device_id,
                    expected_binding_version=profile.binding_version,
                    now=command.now,
                )
                self._require_action_binding(profile, binding)
                trust = await self._device.lock_current(
                    connection,
                    actor_id=command.actor_id,
                    device_id=profile.device_id,
                    binding_id=profile.binding_id,
                    binding_version=profile.binding_version,
                    now=command.now,
                )
                if trust.trust == "revoked":
                    raise PersistentSessionDenied(trust.reason_code)
                locked_receipt = await self._authorize_action_locked(
                    connection,
                    profile=profile,
                    current_context=current_context,
                    binding=binding,
                    trust=trust,
                    actor_id=command.actor_id,
                    capability=receipt.capability.value,
                    session_epoch=command.session_epoch,
                    generation_id=command.generation_id,
                    turn_id=command.turn_id,
                    tool_epoch=command.tool_epoch,
                    data_classification=receipt.data_classification.value,
                    safety_state=receipt.safety_state.value,
                    now=command.now,
                )
                if locked_receipt != receipt:
                    raise PersistentSessionDenied(
                        "Policy receipt is no longer authorized by current evidence"
                    )
                intent = {
                    "session_id": command.session_id,
                    "fence": {
                        "session_id": command.session_id,
                        "session_epoch": command.session_epoch,
                        "generation_id": command.generation_id,
                        "turn_id": command.turn_id,
                        "tool_epoch": command.tool_epoch,
                    },
                    "runtime_profile": profile.model_dump(mode="json"),
                    "policy_receipt": receipt.model_dump(mode="json"),
                    "capability": command.capability,
                    "purpose": command.purpose,
                    "resource_id": command.resource_id,
                    "evidence_refs": list(command.evidence_refs),
                    "idempotency_key": command.idempotency_key,
                    "intent": command.intent,
                    "payload": command.payload,
                    "payload_sha256": command.payload_sha256,
                    "fence_fingerprint": command.fence_fingerprint,
                    "authority_now": command.now.isoformat(),
                }
                return await self._store.commit_tool_effect(
                    connection,
                    intent=intent,
                )
        except (PersistentSessionDenied, SessionRuntimeConflict):
            raise
        except (
            ActionAuthorizationError,
            PolicyReceiptConflictError,
            SessionRuntimeAuthorityUnavailable,
            asyncpg.PostgresError,
            ValueError,
        ) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc

    async def reconcile_tool_effect(
        self,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Return a durable outcome; connection/authority errors propagate."""
        try:
            async with self._store.action_transaction(
                actor_id="reconcile",
                device_id="reconcile",
            ) as connection:
                return await self._store.reconcile_tool_effect(
                    connection,
                    idempotency_key=idempotency_key,
                )
        except (SessionRuntimeConflict, SessionRuntimeAuthorityUnavailable):
            raise
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc

    async def decide(
        self,
        *,
        runtime_profile_id: str,
        capability: CapabilityValue,
        actor_id: str,
        data_classification: str,
        safety_state: str,
        now: datetime,
    ) -> PolicyDecision:
        try:
            async with self._store.read_transaction(actor_id=actor_id) as connection:
                requested = await self._store.profile_by_id(
                    connection,
                    runtime_profile_id=runtime_profile_id,
                )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        if requested is None:
            raise PersistentSessionNotFound(runtime_profile_id)
        self._require_current_profile(requested, now=now)
        if requested.actor_id != actor_id:
            raise PersistentSessionDenied("runtime profile actor mismatch")
        current, current_context = await self.current(
            actor_id=actor_id,
            session_id=requested.session_id,
            now=now,
        )
        if current.runtime_profile_id != requested.runtime_profile_id:
            raise PersistentSessionDenied("runtime profile is stale")
        try:
            async with self._store.action_transaction(
                actor_id=actor_id,
                device_id=requested.device_id,
                subject_id=requested.active_subject_id,
            ) as connection:
                binding = await self._identity.lock_binding(
                    connection,
                    actor_id=actor_id,
                    device_id=requested.device_id,
                    expected_binding_version=requested.binding_version,
                    now=now,
                )
                trust = await self._device.lock_current(
                    connection,
                    actor_id=actor_id,
                    device_id=requested.device_id,
                    binding_id=requested.binding_id,
                    binding_version=requested.binding_version,
                    now=now,
                )
                if trust.trust == "revoked":
                    raise PersistentSessionDenied(trust.reason_code)
                subject = next(
                    (
                        item
                        for item in binding.subjects
                        if item.person_id == requested.active_subject_id
                    ),
                    None,
                )
                if subject is not None and subject.revision != requested.subject_revision:
                    raise PersistentSessionDenied("subject revision is stale")
                purpose = canonical_runtime_decision_purpose(capability)
                discovery = await self._discover_current_consent(
                    connection,
                    actor_id=actor_id,
                    subject_id=requested.active_subject_id,
                    resource_owner_id=requested.active_subject_id,
                    device_id=requested.device_id,
                    binding_id=requested.binding_id,
                    binding_version=requested.binding_version,
                    capability=capability,
                    purpose=purpose,
                    now=now,
                )
                context = PolicyContext(
                    actor_id=actor_id,
                    subject_id=requested.active_subject_id,
                    resource_owner_id=requested.active_subject_id,
                    device_id=requested.device_id,
                    capability=capability,
                    purpose=purpose,
                    declared_device_mode=binding.evidence.declared_mode,
                    current_session_mode=requested.service_mode.value,
                    subject_category=requested.subject_category.value,
                    age_band=requested.age_band.value,
                    speaker_state=requested.speaker_state.value,
                    speaker_confidence=requested.speaker_confidence,
                    device_trust=trust.trust,
                    safety_state=safety_state,
                    jurisdiction="CN",
                    data_classification=data_classification,
                    binding_id=requested.binding_id,
                    binding_version=requested.binding_version,
                    session_id=requested.session_id,
                    session_epoch=requested.session_epoch,
                    runtime_profile_id=requested.runtime_profile_id,
                    subject_revision=requested.subject_revision,
                    evaluated_at=now,
                    idempotency_key=_digest(
                        (
                            runtime_profile_id,
                            actor_id,
                            capability,
                            current_context.session_epoch,
                            current_context.generation_id,
                            current_context.turn_id,
                            current_context.tool_epoch,
                            data_classification,
                            safety_state,
                        )
                    ),
                    binding_evidence=binding.evidence,
                    relationship_evidence=binding.relationships,
                    consent_evidence=discovery.consent_evidence,
                    consent_snapshot_evidence=(
                        discovery.consent_snapshot_evidence
                    ),
                    generation_id=current_context.generation_id,
                    turn_id=current_context.turn_id,
                    tool_epoch=current_context.tool_epoch,
                )
                if capability not in {item.value for item in requested.capabilities}:
                    writer = InMemoryPolicyReceiptWriter()
                    deny_engine = PolicyEngine(
                        policy_version=self._policy.policy_version,
                        receipt_ttl=self._policy.receipt_ttl,
                        receipt_writer=writer,
                    )
                    decision = deny_engine.deny(
                        context,
                        reason_code="capability_not_in_runtime_profile",
                    )
                    receipt = writer.get(decision.receipt_id)
                    if receipt is None:
                        raise PersistentSessionUnavailable("Policy deny receipt was not produced")
                else:
                    decision = self._policy.decide(context)
                    receipt = self._policy.receipt_for(context, decision)
                if receipt.effect.value not in {"allow", "allow_with_obligations"}:
                    await self._receipt_repository.insert_many(connection, (receipt,))
                    await self._store.lock_current(connection, (receipt,))
                    return decision

                async def authorized_noop(
                    _connection: asyncpg.Connection,
                    locked: tuple[PolicyReceiptV2, ...],
                ) -> PolicyDecision:
                    if locked != (receipt,):
                        raise PersistentSessionUnavailable("Policy decision receipt lock mismatch")
                    return decision

                return await self._batch.execute_profile_persist(
                    connection,
                    receipts=(receipt,),
                    requests=(
                        ActionExecutionRequest(
                            receipt_id=receipt.receipt_id,
                            context=context,
                            now=now,
                            consent_authority_proof=(
                                discovery.authority_proof
                            ),
                        ),
                    ),
                    write_callback=authorized_noop,
                )
        except PersistentSessionDenied:
            raise
        except (
            ActionAuthorizationError,
            SessionRuntimeAuthorityUnavailable,
            asyncpg.PostgresError,
            ValueError,
        ) as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc


def build_postgres_session_runtime_service(
    *,
    store: PostgresSessionRuntimeStore,
    signing_key: bytes,
    policy: PolicyEngine | None = None,
    profile_ttl: timedelta = _DEFAULT_PROFILE_TTL,
) -> PostgresSessionRuntimeService:
    identity = _PostgresIdentityAuthority()
    device = _PostgresDeviceAuthority()
    repository = _ActionExecutorReceiptRepository()
    return PostgresSessionRuntimeService(
        store=store,
        signing_key=signing_key,
        policy=policy or PolicyEngine(),
        identity=identity,
        device=device,
        receipt_repository=repository,
        profile_ttl=profile_ttl,
    )


__all__ = [
    "PersistentSessionDenied",
    "PersistentSessionUnavailable",
    "PostgresSessionRuntimeService",
    "StartPersistentSessionCommand",
    "build_postgres_session_runtime_service",
]
