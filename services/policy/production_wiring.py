"""Single production assembly entry for Policy PostgreSQL consumers.

This module is callable production infrastructure, not downstream wiring.
Control still uses its existing in-memory writer, and Control/Memory/Session
main/config/routes are outside Policy's write boundary.  Those call sites must
explicitly install these factories before PostgreSQL decisions, sensitive
writes, or Session batches are end-to-end active.

Public authorization operations are callback-only.  No validated
``PolicyContext``, reusable boolean, bearer token, or verified-facts object is
returned to consumers.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol, TypeVar, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    PolicyReceiptV2,
    PurposeValue,
)

from services.consent.evidence import (
    ConsentEvidence,
    ConsentSnapshot,
)
from services.consent.transaction_authorizer import (
    ConsentFenceMismatchError,
    ExpectedConsentFence,
    TransactionBoundConsentAuthorizer,
)
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    ActionResourceAuthorityAdapter,
    ApprovalAuthorityAdapter,
    BindingAuthorityAdapter,
    CaptureAuthorityAdapter,
    CompositeActionAuthority,
    ConsentAuthorityAdapter,
    MembershipAuthorityAdapter,
    PostgresActionAuthorizer,
    PrincipalAuthorityAdapter,
    ProposalAuthorityAdapter,
    RelationshipAuthorityAdapter,
    WriteCallback,
)
from services.policy.context import (
    CapabilityScope,
    PolicyContext,
    canonical_purpose_for_capability,
    capability_scope_for,
    is_resource_scoped_action,
)
from services.policy.decision_service import PolicyReceiptRepositoryPort
from services.policy.engine import SENSITIVE_CAPABILITIES, PolicyEngine
from services.policy.evidence import (
    ConsentActorKindValue,
    ConsentEvidencePort,
    ConsentParamsPort,
    ConsentSnapshotEvidencePort,
    ConsentStatusValue,
)
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.session_batch import (
    RuntimeProfileAuthorityAdapter,
    SessionPolicyBatchAuthorizer,
)
from services.policy.settings import (
    PostgresDecisionLifecycle,
    PostgresPolicySettings,
    _build_postgres_decision_lifecycle,
)

T = TypeVar("T")


def _decode_jsonb(value: object, field_name: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} is not valid JSON") from exc
    return value


def _jsonb_object(value: object, field_name: str) -> dict[str, object]:
    decoded = _decode_jsonb(value, field_name)
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError(f"{field_name} must be a JSON object")
    return decoded


def _jsonb_array(value: object, field_name: str) -> list[object]:
    decoded = _decode_jsonb(value, field_name)
    if not isinstance(decoded, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return decoded


@dataclass(frozen=True, slots=True)
class ConsentDiscovery:
    """One action-time consent discovery result, locked on the caller txn.

    ``consent_evidence`` carries every current evidence head matching the
    action fence, including revoked/expired current evidence — effectiveness
    is decided by the policy engine, never dropped here.
    ``consent_snapshot_evidence`` carries at most one current snapshot and is
    empty when the subject/binding has no snapshot head yet.  A discovery with
    no evidence is valid state and must fail closed at policy decision time.
    """

    consent_evidence: tuple[ConsentEvidencePort, ...]
    consent_snapshot_evidence: tuple[ConsentSnapshotEvidencePort, ...]
    authority_proof: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _ConsentDiscoveryProof:
    """Opaque proof that discovery locks are held on one exact transaction."""

    issuer: object
    connection: object
    transaction_id: int
    advisory_lock_keys: tuple[int, int]
    action_identity: tuple[object, ...]
    consent_evidence: tuple[ConsentEvidencePort, ...]
    consent_snapshot_evidence: tuple[ConsentSnapshotEvidencePort, ...]


@dataclass(frozen=True, slots=True)
class ConsentEvidenceAdapter:
    """Canonical ``ConsentEvidence`` projected onto the Policy evidence port.

    The adapter is the only seam where the consent-domain grant crosses into
    Policy.  The underlying evidence is built by
    ``ConsentEvidence.from_canonical_dict``, so exact key sets, canonical
    hashes, bounded fields, statuses and capability/purpose pairs were
    already validated by Consent before the port exists.
    """

    _evidence: ConsentEvidence

    @property
    def consent_id(self) -> str:
        return self._evidence.consent_id

    @property
    def version(self) -> int:
        return self._evidence.version

    @property
    def snapshot_id(self) -> str:
        return self._evidence.snapshot_id

    @property
    def status(self) -> ConsentStatusValue:
        return self._evidence.status

    @property
    def subject_id(self) -> str:
        return self._evidence.subject_id

    @property
    def resource_owner_id(self) -> str:
        return self._evidence.resource_owner_id

    @property
    def actor_id(self) -> str:
        return self._evidence.actor_id

    @property
    def actor_kind(self) -> ConsentActorKindValue:
        return self._evidence.actor_kind

    @property
    def device_id(self) -> str | None:
        return self._evidence.device_id

    @property
    def binding_id(self) -> str:
        return self._evidence.binding_id

    @property
    def binding_version(self) -> int:
        return self._evidence.binding_version

    @property
    def capability(self) -> CapabilityValue:
        return self._evidence.capability

    @property
    def purpose(self) -> str:
        return self._evidence.purpose

    @property
    def policy_version(self) -> str:
        return self._evidence.policy_version

    @property
    def evidence_id(self) -> str:
        return self._evidence.evidence_id

    @property
    def offer_id(self) -> str:
        return self._evidence.offer_id

    @property
    def idempotency_key(self) -> str | None:
        return self._evidence.idempotency_key

    @property
    def params(self) -> ConsentParamsPort:
        return self._evidence.params

    @property
    def valid_from(self) -> datetime:
        return self._evidence.valid_from

    @property
    def valid_until(self) -> datetime:
        return self._evidence.valid_until

    @property
    def supersedes_consent_id(self) -> str | None:
        return self._evidence.supersedes_consent_id

    @property
    def superseded_by_consent_id(self) -> str | None:
        return self._evidence.superseded_by_consent_id

    @property
    def canonical_hash(self) -> str:
        return self._evidence.canonical_hash

    def is_effective_at(self, now: datetime) -> bool:
        return self._evidence.is_effective_at(now)

    def matches(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: CapabilityValue,
    ) -> bool:
        return self._evidence.matches(subject_id, binding_id, binding_version, capability)


@dataclass(frozen=True, slots=True)
class ConsentSnapshotEvidenceAdapter:
    """Current Consent snapshot projected onto the Policy snapshot port.

    ``revision`` maps ``ConsentSnapshot.version``; current-ness derives from
    the snapshot's locked binding fact (``is_active_at``).  ``contains``
    requires the exact consent id/version/hash within this snapshot's own
    subject/binding identity, so a snapshot never claims historical evidence
    from a foreign subject or binding.
    """

    _snapshot: ConsentSnapshot

    @property
    def snapshot_id(self) -> str:
        return self._snapshot.snapshot_id

    @property
    def revision(self) -> int:
        return self._snapshot.version

    @property
    def canonical_hash(self) -> str:
        return self._snapshot.canonical_hash

    @property
    def status(self) -> str:
        binding_status = self._snapshot.binding.status
        return "current" if binding_status == "active" else binding_status

    @property
    def subject_id(self) -> str:
        return self._snapshot.subject_id

    @property
    def binding_id(self) -> str:
        return self._snapshot.binding_id

    @property
    def binding_version(self) -> int:
        return self._snapshot.binding_version

    @property
    def grant_refs(self) -> tuple[tuple[str, int, str], ...]:
        return tuple(
            sorted(
                (grant.consent_id, grant.version, grant.canonical_hash)
                for grant in self._snapshot.grants
            )
        )

    @property
    def valid_from(self) -> datetime:
        return self._snapshot.binding.valid_from

    @property
    def valid_until(self) -> datetime:
        return self._snapshot.binding.valid_until

    def is_current_at(self, now: datetime) -> bool:
        return self._snapshot.binding.is_active_at(now)

    def contains(self, evidence: ConsentEvidencePort) -> bool:
        if (
            evidence.subject_id != self._snapshot.subject_id
            or evidence.binding_id != self._snapshot.binding_id
            or evidence.binding_version != self._snapshot.binding_version
        ):
            return False
        return any(
            grant.consent_id == evidence.consent_id
            and grant.version == evidence.version
            and grant.canonical_hash == evidence.canonical_hash
            for grant in self._snapshot.grants
        )


class _ConsentTransactionPort(Protocol):
    async def execute_with_authority(
        self,
        conn: asyncpg.Connection,
        *,
        expected: tuple[ExpectedConsentFence, ...],
        now: object,
        operation: Callable[[asyncpg.Connection], Awaitable[None]],
    ) -> None: ...


class PostgresCurrentConsentAuthorityAdapter:
    """Lock Consent evidence heads and the independent global snapshot head.

    Historical ``ConsentEvidence.snapshot_id`` is never interpreted as the
    current head.  Receipt ids/revisions are matched to
    ``PolicyContext.consent_snapshot_evidence``; each exact grant is then
    proven contained by that current snapshot before Consent's transaction
    authorizer locks both authority-head families on the same connection.
    """

    def __init__(
        self,
        authorizer: _ConsentTransactionPort | None = None,
    ) -> None:
        self._authorizer = authorizer or cast(
            _ConsentTransactionPort, TransactionBoundConsentAuthorizer()
        )
        self._proof_issuer = object()

    @staticmethod
    async def _transaction_id(connection: asyncpg.Connection) -> int:
        try:
            transaction_id = await connection.fetchval("SELECT txid_current()")
        except asyncpg.PostgresError as exc:
            raise ActionAuthorizationError(
                "consent transaction identity is unavailable"
            ) from exc
        if type(transaction_id) is not int or transaction_id < 1:
            raise ActionAuthorizationError("consent transaction identity is invalid")
        return transaction_id

    @staticmethod
    async def _acquire_proof_lock(
        connection: asyncpg.Connection,
    ) -> tuple[int, int]:
        lock_keys = (
            secrets.randbelow(2_147_483_647) + 1,
            secrets.randbelow(2_147_483_647) + 1,
        )
        try:
            await connection.fetchval(
                "SELECT pg_advisory_xact_lock($1::integer, $2::integer)",
                *lock_keys,
            )
        except asyncpg.PostgresError as exc:
            raise ActionAuthorizationError(
                "consent proof transaction lock is unavailable"
            ) from exc
        return lock_keys

    @staticmethod
    async def _proof_lock_held(
        connection: asyncpg.Connection,
        lock_keys: tuple[int, int],
    ) -> bool:
        try:
            held = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_locks
                    WHERE locktype = 'advisory'
                      AND pid = pg_backend_pid()
                      AND granted
                      AND classid = ($1::integer)::oid
                      AND objid = ($2::integer)::oid
                      AND objsubid = 2
                )
                """,
                *lock_keys,
            )
        except asyncpg.PostgresError as exc:
            raise ActionAuthorizationError(
                "consent proof transaction lock cannot be verified"
            ) from exc
        return held is True

    @staticmethod
    def _action_identity(
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
    ) -> tuple[object, ...]:
        return (
            actor_id,
            subject_id,
            resource_owner_id,
            device_id,
            binding_id,
            binding_version,
            capability,
            purpose,
            now,
        )

    async def _reuse_discovery_lock(
        self,
        connection: asyncpg.Connection,
        request: ActionExecutionRequest,
    ) -> None:
        proof = request.consent_authority_proof
        if not isinstance(proof, _ConsentDiscoveryProof):
            raise ActionAuthorizationError("consent discovery proof is invalid")
        if proof.issuer is not self._proof_issuer:
            raise ActionAuthorizationError("consent discovery proof issuer mismatch")
        if proof.connection is not connection:
            raise ActionAuthorizationError("consent discovery proof connection mismatch")
        in_transaction = cast(
            Callable[[], bool] | None,
            getattr(connection, "is_in_transaction", None),
        )
        if in_transaction is None or not in_transaction():
            raise ActionAuthorizationError("consent discovery proof transaction ended")
        if await self._transaction_id(connection) != proof.transaction_id:
            raise ActionAuthorizationError("consent discovery proof transaction mismatch")
        if not await self._proof_lock_held(
            connection,
            proof.advisory_lock_keys,
        ):
            raise ActionAuthorizationError(
                "consent discovery proof locks are no longer held"
            )
        context = request.context
        action_identity = self._action_identity(
            actor_id=context.actor_id,
            subject_id=context.subject_id,
            resource_owner_id=context.resource_owner_id,
            device_id=context.device_id,
            binding_id=context.binding_id,
            binding_version=context.binding_version,
            capability=context.capability,
            purpose=context.purpose,
            now=request.now,
        )
        if action_identity != proof.action_identity:
            raise ActionAuthorizationError("consent discovery proof action mismatch")
        if (
            request.context.consent_evidence != proof.consent_evidence
            or request.context.consent_snapshot_evidence
            != proof.consent_snapshot_evidence
        ):
            raise ActionAuthorizationError("consent discovery proof evidence mismatch")

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[ConsentEvidencePort, ...]:
        receipt_refs = tuple(
            zip(
                receipt.consent_snapshot_ids,
                receipt.consent_snapshot_revisions,
                strict=True,
            )
        )
        current = {
            (item.snapshot_id, item.revision): item
            for item in request.context.consent_snapshot_evidence
        }
        if not receipt_refs or set(receipt_refs) != set(current):
            raise ActionAuthorizationError(
                "receipt/current global consent snapshot heads mismatch"
            )
        expected: list[ExpectedConsentFence] = []
        for evidence in request.context.consent_evidence:
            snapshot = next(
                (
                    current[identity]
                    for identity in sorted(current)
                    if current[identity].contains(evidence)
                ),
                None,
            )
            if snapshot is None:
                raise ActionAuthorizationError(
                    "current global snapshot lacks historical consent grant"
                )
            expected.append(
                ExpectedConsentFence(
                    request_actor_id=receipt.actor_id,
                    evidence_actor_id=evidence.actor_id,
                    consent_id=evidence.consent_id,
                    revision=evidence.version,
                    canonical_hash=evidence.canonical_hash,
                    current_snapshot_id=snapshot.snapshot_id,
                    current_snapshot_revision=snapshot.revision,
                    current_snapshot_hash=snapshot.canonical_hash,
                    subject_id=evidence.subject_id,
                    binding_id=evidence.binding_id,
                    binding_version=evidence.binding_version,
                    capability=evidence.capability,
                    purpose=cast(PurposeValue, evidence.purpose),
                )
            )
        if not expected:
            raise ActionAuthorizationError("receipted consent head has no exact grant")

        if request.consent_authority_proof is not None:
            await self._reuse_discovery_lock(connection, request)
            return request.context.consent_evidence

        async def locked(_connection: asyncpg.Connection) -> None:
            return None

        try:
            await self._authorizer.execute_with_authority(
                connection,
                expected=tuple(expected),
                now=request.now,
                operation=locked,
            )
        except ConsentFenceMismatchError as exc:
            raise ActionAuthorizationError(str(exc)) from exc
        return request.context.consent_evidence

    async def discover_current(
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
        """Lock and read the current Consent heads for one action fence.

        Must run inside the caller-owned action transaction (the Session
        action-fence transaction); the adapter never starts, commits or
        rolls back a transaction.  All payloads are canonical-decoded by
        Consent's strict readers — unknown keys, tampered hashes or invalid
        capability/purpose pairs raise ``ValueError`` and the caller fails
        closed.  Raw table rows never escape this method.
        """
        in_transaction = cast(
            Callable[[], bool] | None,
            getattr(connection, "is_in_transaction", None),
        )
        if in_transaction is None or not in_transaction():
            raise ActionAuthorizationError(
                "consent discovery requires an already-open caller transaction"
            )
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        try:
            row = await connection.fetchrow(
                "SELECT consent_json, snapshot_json "
                "FROM consent_discover_action_fence($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                actor_id,
                subject_id,
                resource_owner_id,
                device_id,
                binding_id,
                binding_version,
                capability,
                purpose,
                now,
            )
        except asyncpg.PostgresError as exc:
            raise ActionAuthorizationError(str(exc)) from exc
        if row is None:
            raise ActionAuthorizationError("consent discovery returned no state")
        consent_evidence: list[ConsentEvidencePort] = []
        for raw in _jsonb_array(row["consent_json"], "consent_json"):
            evidence = ConsentEvidence.from_canonical_dict(
                _jsonb_object(raw, "consent_json[]")
            )
            consent_evidence.append(ConsentEvidenceAdapter(evidence))
        consent_snapshot_evidence: list[ConsentSnapshotEvidencePort] = []
        if row["snapshot_json"] is not None:
            snapshot = ConsentSnapshot.from_canonical_dict(
                _jsonb_object(row["snapshot_json"], "snapshot_json")
            )
            consent_snapshot_evidence.append(ConsentSnapshotEvidenceAdapter(snapshot))
        transaction_id = await self._transaction_id(connection)
        advisory_lock_keys = await self._acquire_proof_lock(connection)
        action_identity = self._action_identity(
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
        locked_consents = tuple(consent_evidence)
        locked_snapshots = tuple(consent_snapshot_evidence)
        return ConsentDiscovery(
            consent_evidence=locked_consents,
            consent_snapshot_evidence=locked_snapshots,
            authority_proof=_ConsentDiscoveryProof(
                issuer=self._proof_issuer,
                connection=connection,
                transaction_id=transaction_id,
                advisory_lock_keys=advisory_lock_keys,
                action_identity=action_identity,
                consent_evidence=locked_consents,
                consent_snapshot_evidence=locked_snapshots,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProductionAuthorityAdapters:
    """Typed cross-service head-lock adapters; absent required stages deny."""

    principal: PrincipalAuthorityAdapter | None = None
    consent: ConsentAuthorityAdapter | None = None
    relationship: RelationshipAuthorityAdapter | None = None
    membership: MembershipAuthorityAdapter | None = None
    binding: BindingAuthorityAdapter | None = None
    proposal: ProposalAuthorityAdapter | None = None
    action: ActionResourceAuthorityAdapter | None = None
    approvals: ApprovalAuthorityAdapter | None = None
    capture: CaptureAuthorityAdapter | None = None


class SensitiveWriteService:
    """Mint, persist, revalidate and write on one caller-owned transaction."""

    def __init__(
        self,
        *,
        authorizer: PostgresActionAuthorizer,
        repository: ConnectionBoundPolicyReceiptRepository | None = None,
        engine: PolicyEngine | None = None,
        consent_discovery: PostgresCurrentConsentAuthorityAdapter | None = None,
    ) -> None:
        self._authorizer = authorizer
        self._repository = repository or ConnectionBoundPolicyReceiptRepository()
        self._engine = engine or PolicyEngine()
        self._consent_discovery = consent_discovery

    async def execute(
        self,
        connection: asyncpg.Connection,
        context: PolicyContext,
        write_callback: WriteCallback[T],
    ) -> T:
        in_transaction = cast(
            Callable[[], bool] | None,
            getattr(connection, "is_in_transaction", None),
        )
        if in_transaction is None or not in_transaction():
            raise ActionAuthorizationError(
                "sensitive write requires an already-open caller transaction"
            )
        if context.action_resource_fence is None:
            raise ActionAuthorizationError(
                "sensitive write requires an explicit canonical action fence"
            )
        decision_context = context
        consent_authority_proof: object | None = None
        if (
            context.capability in SENSITIVE_CAPABILITIES
            and context.capability != "crisis_notification"
            and self._consent_discovery is not None
        ):
            discovery = await self._consent_discovery.discover_current(
                connection,
                actor_id=context.actor_id,
                subject_id=context.subject_id,
                resource_owner_id=context.resource_owner_id,
                device_id=context.device_id,
                binding_id=context.binding_id,
                binding_version=context.binding_version,
                capability=context.capability,
                purpose=context.purpose,
                now=context.evaluated_at,
            )
            decision_context = replace(
                context,
                consent_evidence=discovery.consent_evidence,
                consent_snapshot_evidence=discovery.consent_snapshot_evidence,
            )
            consent_authority_proof = discovery.authority_proof
        decision = self._engine.decide(decision_context)
        if decision.effect not in {"allow", "allow_with_obligations"}:
            raise ActionAuthorizationError("current policy decision denies the write")
        receipt = self._engine.receipt_for(decision_context, decision)
        if not receipt.exact_fence:
            raise ActionAuthorizationError("sensitive write requires an exact receipt")
        await self._repository.insert_many(connection, (receipt,))
        return await self._authorizer.execute_authorized(
            connection,
            ActionExecutionRequest(
                receipt_id=receipt.receipt_id,
                context=decision_context,
                now=decision_context.evaluated_at,
                consent_authority_proof=consent_authority_proof,
            ),
            write_callback,
        )


def build_postgres_decision_lifecycle(
    settings: PostgresPolicySettings,
    *,
    engine: PolicyEngine | None = None,
    repository: PolicyReceiptRepositoryPort | None = None,
) -> PostgresDecisionLifecycle:
    return _build_postgres_decision_lifecycle(
        settings,
        engine=engine,
        repository=repository,
    )


def build_composite_action_authority(
    adapters: ProductionAuthorityAdapters,
) -> CompositeActionAuthority:
    return CompositeActionAuthority(
        principal=adapters.principal,
        consent=adapters.consent,
        relationship=adapters.relationship,
        membership=adapters.membership,
        binding=adapters.binding,
        proposal=adapters.proposal,
        action=adapters.action,
        approvals=adapters.approvals,
        capture=adapters.capture,
    )


def build_sensitive_write_service(
    adapters: ProductionAuthorityAdapters,
    *,
    repository: ConnectionBoundPolicyReceiptRepository | None = None,
    engine: PolicyEngine | None = None,
) -> SensitiveWriteService:
    return SensitiveWriteService(
        authorizer=PostgresActionAuthorizer(
            authority=build_composite_action_authority(adapters)
        ),
        repository=repository,
        engine=engine,
        consent_discovery=(
            adapters.consent
            if isinstance(
                adapters.consent,
                PostgresCurrentConsentAuthorityAdapter,
            )
            else None
        ),
    )


def build_session_batch_service(
    adapters: ProductionAuthorityAdapters,
    *,
    profile_authority: RuntimeProfileAuthorityAdapter | None,
    repository: ConnectionBoundPolicyReceiptRepository | None = None,
) -> SessionPolicyBatchAuthorizer:
    return SessionPolicyBatchAuthorizer(
        repository=repository or ConnectionBoundPolicyReceiptRepository(),
        action_authorizer=PostgresActionAuthorizer(
            authority=build_composite_action_authority(adapters)
        ),
        profile_authority=profile_authority,
    )


__all__ = [
    "CapabilityScope",
    "PostgresCurrentConsentAuthorityAdapter",
    "PostgresDecisionLifecycle",
    "PostgresPolicySettings",
    "ProductionAuthorityAdapters",
    "SensitiveWriteService",
    "build_composite_action_authority",
    "build_postgres_decision_lifecycle",
    "build_sensitive_write_service",
    "build_session_batch_service",
    "canonical_purpose_for_capability",
    "capability_scope_for",
    "is_resource_scoped_action",
]
