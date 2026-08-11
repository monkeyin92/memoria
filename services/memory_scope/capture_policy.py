"""Production Policy assembly for one MemoryScope capture transaction.

The caller owns a ``memoria_action_executor`` transaction.  This module
locks the current Session, Identity binding/relationships, Device trust,
Consent heads, capture evidence and action resource on that same connection,
builds one exact ``PolicyContext``, and returns a ``SensitiveWriteService``
whose callback is the only place a memory row may be committed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceDeclaredModeValue,
    PolicyActionResourceFence,
    PolicyReceiptV2,
)

from services.consent.evidence import (
    RelationshipEvidence,
    RelationshipStatusValue,
    RelationTypeValue,
)
from services.memory_scope.domain import MemoryWriteDraft, ResolutionContext
from services.memory_scope.repository import MemoryAuthoritySnapshot
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    PostgresActionAuthorizer,
)
from services.policy.action_fence import (
    DEFAULT_ACTION_FENCE_TTL,
    build_action_resource_fence,
)
from services.policy.context import PolicyContext
from services.policy.evidence import (
    BindingEvidencePort,
    BindingStatusValue,
    CaptureEvidencePort,
    RelationshipEvidencePort,
)
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.production_wiring import (
    PostgresCurrentConsentAuthorityAdapter,
    ProductionAuthorityAdapters,
    SensitiveWriteService,
    build_composite_action_authority,
)
from services.policy.receipts import PolicyReceiptConflictError

_NO_EXPIRY = datetime(9999, 12, 31, tzinfo=UTC)


class MemoryCapturePolicyContextBuilderPort(Protocol):
    async def build(
        self,
        connection: asyncpg.Connection,
        *,
        snapshot: MemoryAuthoritySnapshot,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        actor_subject_id: str,
    ) -> PolicyContext: ...


@dataclass(frozen=True, slots=True)
class MemoryCapturePolicyAssembly:
    sensitive_write: SensitiveWriteService
    context_builder: MemoryCapturePolicyContextBuilderPort


@dataclass(frozen=True, slots=True)
class _BindingEvidence:
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
    evidence: _BindingEvidence
    relationships: tuple[RelationshipEvidencePort, ...]


@dataclass(frozen=True, slots=True)
class _CaptureEvidence:
    evidence_id: str
    revision: int
    canonical_hash: str
    status: str
    subject_id: str
    binding_id: str
    binding_version: int
    valid_from: datetime
    valid_until: datetime

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until


class _ActionExecutorReceiptRepository(ConnectionBoundPolicyReceiptRepository):
    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        if not connection.is_in_transaction():
            raise RuntimeError("policy receipt insert requires a caller transaction")
        for receipt in sorted(receipts, key=lambda item: item.receipt_id):
            try:
                raw = await connection.fetchval(
                    "SELECT action_policy_insert_receipt($1::jsonb)",
                    json.dumps(receipt.model_dump(mode="json")),
                )
            except asyncpg.PostgresError as exc:
                _raise_database_error(exc, field="policy receipt insert")
            current = _policy_receipt(raw, field="policy receipt insert")
            if current != receipt:
                raise PolicyReceiptConflictError(
                    "policy receipt id is immutable and content differs"
                )


async def _lock_receipt(
    connection: asyncpg.Connection,
    receipt_id: str,
) -> PolicyReceiptV2 | None:
    try:
        raw = await connection.fetchval(
            "SELECT action_policy_lock_receipt($1)", receipt_id
        )
    except asyncpg.PostgresError as exc:
        _raise_database_error(exc, field="policy receipt lock")
    return None if raw is None else _policy_receipt(raw, field="policy receipt lock")


class _PrincipalAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> str:
        try:
            session_current = await connection.fetchval(
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
            trust_raw = await connection.fetchval(
                "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
                receipt.actor_id,
                receipt.device_id,
                receipt.binding_id,
                receipt.binding_version,
                request.now,
            )
            actor = await connection.fetchval(
                "SELECT current_setting('app.authenticated_actor', true)"
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Session/Device principal authority")
        trust = _json_object(trust_raw, field="device authority")
        if (
            session_current is not True
            or actor != receipt.actor_id
            or trust.get("device_trust") != request.context.device_trust
        ):
            raise ActionAuthorizationError("authenticated principal authority changed")
        return cast(str, actor)


class _BindingAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> BindingEvidencePort:
        binding = await _lock_binding(
            connection,
            actor_id=receipt.actor_id,
            device_id=receipt.device_id,
            binding_version=receipt.binding_version,
            now=request.now,
        )
        evidence = binding.evidence
        if (
            evidence.binding_id != receipt.binding_id
            or evidence.canonical_hash != receipt.binding_canonical_hash
        ):
            raise ActionAuthorizationError("current binding evidence changed")
        return evidence


class _RelationshipAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[RelationshipEvidencePort, ...]:
        binding = await _lock_binding(
            connection,
            actor_id=receipt.actor_id,
            device_id=receipt.device_id,
            binding_version=receipt.binding_version,
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
            item
            for item in binding.relationships
            if (item.snapshot_id, item.revision) in expected
        )
        if {(item.snapshot_id, item.revision) for item in current} != expected:
            raise ActionAuthorizationError("current relationship evidence changed")
        return current


class _CaptureAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[CaptureEvidencePort, ...]:
        fence = receipt.action_resource_fence
        current = await _lock_capture_evidence(
            connection,
            evidence_ids=tuple(fence.capture_evidence_ids),
            subject_id=cast(str, receipt.subject_id),
            binding_id=receipt.binding_id,
            binding_version=receipt.binding_version,
            now=request.now,
        )
        if tuple(sorted(item.evidence_id for item in current)) != tuple(
            sorted(fence.capture_evidence_ids)
        ):
            raise ActionAuthorizationError("current capture evidence changed")
        return current


class _ActionResourceAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyActionResourceFence:
        fence = receipt.action_resource_fence
        canonical_action_sha256 = _capture_action_digest(fence.action_resource_id)
        try:
            current = await connection.fetchval(
                """
                SELECT memory_capture_lock_action_resource(
                    $1, $2, $3, $4::text[], $5, $6
                )
                """,
                receipt.capability,
                fence.action_resource_id,
                receipt.subject_id,
                list(fence.capture_evidence_ids),
                canonical_action_sha256,
                fence.action_revision,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="memory capture action authority")
        if current is not True or fence != request.context.action_resource_fence:
            raise ActionAuthorizationError("memory capture action resource changed")
        return fence


class PostgresMemoryCapturePolicyContextBuilder:
    def __init__(self, consent: PostgresCurrentConsentAuthorityAdapter) -> None:
        self._consent = consent

    async def build(
        self,
        connection: asyncpg.Connection,
        *,
        snapshot: MemoryAuthoritySnapshot,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        actor_subject_id: str,
    ) -> PolicyContext:
        now = datetime.now(UTC)
        fence = context.fence
        if fence is None or fence.is_expired(now):
            raise ActionAuthorizationError("memory capture session fence is unavailable")
        if (
            actor_subject_id != snapshot.active_subject_id
            or actor_subject_id != fence.actor_subject_id
            or actor_subject_id != fence.active_subject_id
        ):
            raise ActionAuthorizationError(
                "memory capture requires the authenticated current subject"
            )
        if context.subject.active_subject_id != snapshot.active_subject_id:
            raise ActionAuthorizationError("memory capture subject snapshot mismatch")
        binding = await _lock_binding(
            connection,
            actor_id=actor_subject_id,
            device_id=fence.device_id,
            binding_version=fence.binding_version,
            now=now,
        )
        try:
            trust_raw = await connection.fetchval(
                "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
                actor_subject_id,
                fence.device_id,
                fence.binding_id,
                fence.binding_version,
                now,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Device trust authority")
        trust = _text(
            _json_object(trust_raw, field="device trust").get("device_trust"),
            field="device_trust",
        )
        captures = await _lock_capture_evidence(
            connection,
            evidence_ids=draft.source_evidence_ids,
            subject_id=actor_subject_id,
            binding_id=fence.binding_id,
            binding_version=fence.binding_version,
            now=now,
        )
        discovery = await self._consent.discover_current(
            connection,
            actor_id=actor_subject_id,
            subject_id=actor_subject_id,
            resource_owner_id=actor_subject_id,
            device_id=fence.device_id,
            binding_id=fence.binding_id,
            binding_version=fence.binding_version,
            capability="memory_capture",
            purpose="memory_capture",
            now=now,
        )
        if len(discovery.consent_snapshot_evidence) != 1:
            raise ActionAuthorizationError(
                "memory capture requires one current Consent snapshot"
            )
        consent_snapshot = discovery.consent_snapshot_evidence[0]
        if (
            context.consent.snapshot_id != consent_snapshot.snapshot_id
            or snapshot.consent_snapshot_id != consent_snapshot.snapshot_id
        ):
            raise ActionAuthorizationError("memory capture Consent snapshot changed")
        draft_sha256 = _sha256(
            {
                "content": draft.content,
                "memory_type": draft.memory_type,
                "confidence": draft.confidence,
                "payload": draft.payload,
            }
        )
        action_sha256 = _sha256(
            {
                "subject_id": actor_subject_id,
                "scope": context.requested_scope.value,
                "source_evidence_ids": sorted(draft.source_evidence_ids),
                "draft_sha256": draft_sha256,
                "session_id": fence.session_id,
                "session_epoch": fence.epoch,
                "generation_id": snapshot.generation_id,
                "turn_id": snapshot.turn_id,
                "tool_epoch": snapshot.tool_epoch,
            }
        )
        action_resource_id = f"capture:{action_sha256}"
        valid_until = min(
            now + DEFAULT_ACTION_FENCE_TTL,
            fence.valid_until or now + DEFAULT_ACTION_FENCE_TTL,
        )
        if valid_until <= now:
            raise ActionAuthorizationError("memory capture action fence expired")
        action_fence = build_action_resource_fence(
            capability="memory_capture",
            purpose="memory_capture",
            action_resource_id=action_resource_id,
            action_revision=snapshot.profile_revision,
            generation_id=snapshot.generation_id,
            turn_id=snapshot.turn_id,
            tool_epoch=snapshot.tool_epoch,
            issued_at=now,
            valid_until=valid_until,
            capture_evidence_records=tuple(
                (item.evidence_id, item.revision, item.canonical_hash)
                for item in captures
            ),
            consent_snapshot_id=consent_snapshot.snapshot_id,
            consent_snapshot_revision=consent_snapshot.revision,
            consent_snapshot_hash=consent_snapshot.canonical_hash,
        )
        return PolicyContext(
            actor_id=actor_subject_id,
            subject_id=actor_subject_id,
            resource_owner_id=actor_subject_id,
            device_id=fence.device_id,
            capability="memory_capture",
            purpose="memory_capture",
            declared_device_mode=binding.evidence.declared_mode,
            current_session_mode=snapshot.service_mode,
            subject_category=snapshot.subject_category,
            age_band=snapshot.age_band,
            speaker_state=snapshot.speaker_state,
            speaker_confidence=snapshot.speaker_confidence,
            device_trust=trust,
            safety_state="normal",
            jurisdiction="CN",
            data_classification="private",
            binding_id=fence.binding_id,
            binding_version=fence.binding_version,
            session_id=fence.session_id,
            session_epoch=fence.epoch,
            runtime_profile_id=fence.runtime_profile_id,
            subject_revision=fence.subject_revision,
            evaluated_at=now,
            idempotency_key=action_resource_id,
            consent_evidence=discovery.consent_evidence,
            consent_snapshot_evidence=discovery.consent_snapshot_evidence,
            relationship_evidence=binding.relationships,
            binding_evidence=binding.evidence,
            generation_id=snapshot.generation_id,
            turn_id=snapshot.turn_id,
            tool_epoch=snapshot.tool_epoch,
            action_resource_fence=action_fence,
            capture_evidence=captures,
        )


def build_memory_capture_policy_assembly() -> MemoryCapturePolicyAssembly:
    consent = PostgresCurrentConsentAuthorityAdapter()
    authority = build_composite_action_authority(
        ProductionAuthorityAdapters(
            principal=_PrincipalAuthority(),
            consent=consent,
            relationship=_RelationshipAuthority(),
            binding=_BindingAuthority(),
            action=_ActionResourceAuthority(),
            capture=_CaptureAuthority(),
        )
    )
    sensitive_write = SensitiveWriteService(
        authorizer=PostgresActionAuthorizer(
            authority=authority,
            receipt_locker=_lock_receipt,
        ),
        repository=_ActionExecutorReceiptRepository(),
        consent_discovery=consent,
    )
    return MemoryCapturePolicyAssembly(
        sensitive_write=sensitive_write,
        context_builder=PostgresMemoryCapturePolicyContextBuilder(consent),
    )


async def _lock_binding(
    connection: asyncpg.Connection,
    *,
    actor_id: str,
    device_id: str,
    binding_version: int,
    now: datetime,
) -> _LockedBinding:
    try:
        raw = await connection.fetchval(
            "SELECT action_identity_lock_binding($1, $2, $3, $4)",
            actor_id,
            device_id,
            binding_version,
            now,
        )
    except asyncpg.PostgresError as exc:
        _raise_database_error(exc, field="Identity binding authority")
    if raw is None:
        raise ActionAuthorizationError("Identity binding authority is unavailable")
    value = _json_object(raw, field="Identity binding authority")
    binding_id = _text(value.get("binding_id"), field="binding_id")
    version = _int(value.get("binding_version"), field="binding_version")
    locked_device_id = _text(value.get("device_id"), field="device_id")
    declared_mode = _text(value.get("declared_mode"), field="declared_mode")
    if declared_mode not in {
        "parent_for_child",
        "self_use",
        "child_for_parent",
        "family_shared",
    }:
        raise ActionAuthorizationError("Identity binding mode is invalid")
    valid_from = _time(value.get("valid_from"), field="binding.valid_from")
    valid_until_raw = value.get("valid_until")
    valid_until = (
        _NO_EXPIRY
        if valid_until_raw is None
        else _time(valid_until_raw, field="binding.valid_until")
    )
    evidence = _BindingEvidence(
        binding_id=binding_id,
        version=version,
        device_id=locked_device_id,
        status="active",
        declared_mode=cast(DeviceDeclaredModeValue, declared_mode),
        valid_from=valid_from,
        valid_until=valid_until,
        canonical_hash=_sha256(
            {
                "binding_id": binding_id,
                "version": version,
                "device_id": locked_device_id,
                "status": "active",
                "declared_mode": declared_mode,
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
            }
        ),
    )
    relationships_raw = value.get("relationships")
    if not isinstance(relationships_raw, list):
        raise ActionAuthorizationError("Identity relationship authority is unavailable")
    relationships: list[RelationshipEvidencePort] = []
    for item in relationships_raw:
        relation = _json_object(item, field="Identity relationship")
        updated_at = _time(relation.get("updated_at"), field="relationship.updated_at")
        relation_valid_from = _time(
            relation.get("valid_from"), field="relationship.valid_from"
        )
        relation_valid_until_raw = relation.get("valid_until")
        relation_valid_until = (
            _NO_EXPIRY
            if relation_valid_until_raw is None
            else _time(
                relation_valid_until_raw,
                field="relationship.valid_until",
            )
        )
        relationship_id = _text(
            relation.get("relationship_id"), field="relationship_id"
        )
        revision = max(1, int(updated_at.timestamp() * 1_000_000))
        relationships.append(
            RelationshipEvidence(
                relationship_id=relationship_id,
                snapshot_id=f"{relationship_id}:v{revision}",
                revision=revision,
                relation_type=cast(
                    RelationTypeValue,
                    _text(relation.get("relation_type"), field="relation_type"),
                ),
                status=cast(
                    RelationshipStatusValue,
                    _text(relation.get("status"), field="relationship.status"),
                ),
                source_person_id=_text(
                    relation.get("source_person_id"), field="relationship.source"
                ),
                target_person_id=_text(
                    relation.get("target_person_id"), field="relationship.target"
                ),
                binding_id=binding_id,
                valid_from=relation_valid_from,
                valid_until=relation_valid_until,
            )
        )
    return _LockedBinding(evidence=evidence, relationships=tuple(relationships))


async def _lock_capture_evidence(
    connection: asyncpg.Connection,
    *,
    evidence_ids: tuple[str, ...],
    subject_id: str,
    binding_id: str,
    binding_version: int,
    now: datetime,
) -> tuple[_CaptureEvidence, ...]:
    if not evidence_ids:
        raise ActionAuthorizationError("memory capture requires source evidence")
    try:
        raw = await connection.fetchval(
            """
            SELECT memory_shared_lock_capture_evidence(
                $1::text[], $2, $3, $4, $5
            )
            """,
            list(evidence_ids),
            subject_id,
            binding_id,
            binding_version,
            now,
        )
    except asyncpg.PostgresError as exc:
        _raise_database_error(exc, field="memory capture evidence authority")
    values = _json_array(raw, field="memory capture evidence authority")
    captures: list[_CaptureEvidence] = []
    for item in values:
        value = _json_object(item, field="memory capture evidence")
        valid_until_raw = value.get("valid_until")
        captures.append(
            _CaptureEvidence(
                evidence_id=_text(value.get("evidence_id"), field="evidence_id"),
                revision=_int(value.get("revision"), field="evidence_revision"),
                canonical_hash=_text(
                    value.get("canonical_hash"), field="evidence_hash"
                ),
                status=_text(value.get("status"), field="evidence_status"),
                subject_id=_text(value.get("subject_id"), field="evidence_subject"),
                binding_id=_text(value.get("binding_id"), field="evidence_binding"),
                binding_version=_int(
                    value.get("binding_version"), field="evidence_binding_version"
                ),
                valid_from=_time(
                    value.get("valid_from"), field="evidence_valid_from"
                ),
                valid_until=(
                    _NO_EXPIRY
                    if valid_until_raw is None
                    else _time(valid_until_raw, field="evidence_valid_until")
                ),
            )
        )
    if tuple(sorted(item.evidence_id for item in captures)) != tuple(
        sorted(evidence_ids)
    ):
        raise ActionAuthorizationError("memory capture evidence set mismatch")
    return tuple(captures)


def _capture_action_digest(action_resource_id: str) -> str:
    prefix = "capture:"
    digest = action_resource_id.removeprefix(prefix)
    if (
        not action_resource_id.startswith(prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ActionAuthorizationError("memory capture action id is invalid")
    return digest


def _policy_receipt(value: object, *, field: str) -> PolicyReceiptV2:
    try:
        if isinstance(value, str):
            return PolicyReceiptV2.model_validate_json(value)
        return PolicyReceiptV2.model_validate(value)
    except ValueError as exc:
        raise ActionAuthorizationError(f"{field} is invalid") from exc


def _json_object(value: object, *, field: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ActionAuthorizationError(f"{field} is invalid JSON") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ActionAuthorizationError(f"{field} is not a JSON object")
    return value


def _json_array(value: object, *, field: str) -> list[object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ActionAuthorizationError(f"{field} is invalid JSON") from exc
    if not isinstance(value, list):
        raise ActionAuthorizationError(f"{field} is not a JSON array")
    return value


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionAuthorizationError(f"{field} is unavailable")
    return value


def _int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ActionAuthorizationError(f"{field} is unavailable")
    return value


def _time(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ActionAuthorizationError(f"{field} is invalid") from exc
    else:
        raise ActionAuthorizationError(f"{field} is unavailable")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ActionAuthorizationError(f"{field} must be timezone-aware")
    return result.astimezone(UTC)


def _sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ActionAuthorizationError("memory capture payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _raise_database_error(exc: asyncpg.PostgresError, *, field: str) -> None:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "SR403":
        raise ActionAuthorizationError(f"{field} denied") from exc
    if sqlstate in {"SR409", "SR412"}:
        raise ActionAuthorizationError(f"{field} changed") from exc
    raise ActionAuthorizationError(f"{field} is unavailable") from exc


__all__ = [
    "MemoryCapturePolicyAssembly",
    "MemoryCapturePolicyContextBuilderPort",
    "PostgresMemoryCapturePolicyContextBuilder",
    "build_memory_capture_policy_assembly",
]
