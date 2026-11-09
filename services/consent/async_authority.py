"""Asynchronous consent authority over :class:`AsyncConsentStorePort`.

Split out of ``services.consent.authority`` so the synchronous authority and
the async one can be read on their own.  ``services.consent.authority`` keeps
the public path with an explicit re-export; behavior is unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from packages.contracts.generated.python.multi_subject_contracts import PurposeValue

from services.consent.consent_rules import (
    ConsentDeniedError,
    ConsentOperationResult,
    _async_build_snapshot,
    _async_replay,
    _grant_content_hash,
    _outbox_payload,
    _snapshot_unchanged,
    _superseded_version,
    _termination_content_hash,
    _utc,
    _with_new_identity,
    verify_grant,
    verify_revocation,
)
from services.consent.events import AuditEntry, ConsentOutboxEvent
from services.consent.evidence import (
    ActorKind,
    BindingEvidence,
    ConsentEvidence,
    ConsentOffer,
    ConsentSnapshot,
    RelationshipEvidence,
)
from services.consent.store import (
    AsyncConsentStorePort,
    ConsentConflictError,
    ConsentConsistencyError,
    ConsentNotFoundError,
    IdempotencyRecord,
)

if TYPE_CHECKING:
    from services.consent.authority import AsyncEvidenceResolverPort, SubjectProof


class AsyncConsentAuthority:
    """Asynchronous consent authority over an :class:`AsyncConsentStorePort`."""

    def __init__(
        self,
        store: AsyncConsentStorePort,
        *,
        resolver: AsyncEvidenceResolverPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._now = now or (lambda: datetime.now(UTC))

    async def create_offer(
        self,
        *,
        capability: str,
        subject_id: str,
        actor_id: str,
        resource_owner_id: str,
        purpose: PurposeValue,
        params: object,
        valid_from: datetime,
        valid_until: datetime,
        policy_version: str,
        offer_id: str | None = None,
        created_at: datetime | None = None,
        version: int = 1,
        status: str = "active",
        issuer: str = "consent_authority",
        supersedes_offer_id: str | None = None,
    ) -> ConsentOffer:
        from services.consent.evidence import ConsentParams

        if not isinstance(params, ConsentParams):
            raise ValueError("params must be a ConsentParams instance")
        resolved_offer_id = offer_id or str(uuid.uuid4())
        uow = await self._store.transaction()
        try:
            await uow.lock_offer_head(resolved_offer_id, actor_id, subject_id)
            existing = await uow.offer_by_id(resolved_offer_id, version)
            issued_at = created_at or (existing.created_at if existing is not None else self._now())
            offer = ConsentOffer(
                offer_id=resolved_offer_id,
                version=version,
                status=status,  # type: ignore[arg-type]
                capability=capability,  # type: ignore[arg-type]
                subject_id=subject_id,
                actor_id=actor_id,
                resource_owner_id=resource_owner_id,
                purpose=purpose,
                params=params,
                valid_from=_utc(valid_from, "valid_from"),
                valid_until=_utc(valid_until, "valid_until"),
                created_at=_utc(issued_at, "created_at"),
                issuer=issuer,
                policy_version=policy_version,
                supersedes_offer_id=supersedes_offer_id,
            )
            if existing is not None:
                if existing.canonical_hash != offer.canonical_hash:
                    raise ConsentConflictError(
                        "offer id/version reused with different canonical content"
                    )
                await uow.commit()
                return existing
            latest = await uow.latest_offer(offer.offer_id)
            if latest is None and offer.version != 1:
                raise ConsentConflictError("offer version must start at 1")
            if latest is not None and latest.version != offer.version - 1:
                raise ConsentConflictError("offer version must be monotonically incremented")
            if latest is not None and offer.supersedes_offer_id != latest.offer_id:
                raise ConsentConflictError("offer version must reference the superseded offer")
            await uow.append_offer(offer)
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memoria:consent-offer:{offer.offer_id}:{offer.version}:{offer.canonical_hash}",
                )
            )
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_offer",
                aggregate_id=offer.offer_id,
                version=offer.version,
                payload=_outbox_payload(
                    ("actor_id", offer.actor_id),
                    ("binding_id", "__consent_offer__"),
                    ("capability", offer.capability),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("status", offer.status),
                    ("subject_id", offer.subject_id),
                ),
                created_at=offer.created_at,
            )
            await uow.append_outbox(event)
            await uow.append_audit(
                AuditEntry(
                    audit_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"memoria:consent-offer-audit:{offer.offer_id}:{offer.version}:{offer.canonical_hash}",
                        )
                    ),
                    event_id=event_id,
                    action="offer_create",
                    actor_id=offer.actor_id,
                    subject_id=offer.subject_id,
                    consent_id=None,
                    snapshot_id=None,
                    payload=_outbox_payload(
                        ("offer_hash", offer.canonical_hash),
                        ("offer_id", offer.offer_id),
                        ("offer_version", str(offer.version)),
                    ),
                    created_at=offer.created_at,
                )
            )
            await uow.commit()
            return offer
        except BaseException:
            await uow.rollback()
            raise

    async def grant(
        self,
        offer: ConsentOffer | str,
        *,
        expected_version: int | None = None,
        subject: SubjectProof,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
        actor_kind: ActorKind | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        grant_at = _utc(now or self._now(), "now")
        offer_id = offer.offer_id if isinstance(offer, ConsentOffer) else offer
        requested_version = (
            expected_version
            if expected_version is not None
            else (offer.version if isinstance(offer, ConsentOffer) else None)
        )
        uow = await self._store.transaction()
        try:
            stored_offer = await uow.latest_offer(offer_id)
            if stored_offer is None:
                raise ConsentDeniedError("offer_not_found", "offer is not stored by the authority")
            if requested_version is None or stored_offer.version != requested_version:
                raise ConsentConflictError("offer version does not match latest version")
            offer = stored_offer
            if offer.status != "active":
                raise ConsentDeniedError("offer_inactive", "offer status is not active")
            if grant_at >= offer.valid_until:
                raise ConsentDeniedError("offer_expired", "the offer window has expired")
            if grant_at < offer.valid_from:
                raise ConsentDeniedError("offer_not_yet_valid", "the offer window has not opened")
            if self._resolver is None:
                raise ConsentDeniedError(
                    "evidence_unresolved",
                    "evidence_unresolved: an authoritative evidence resolver is required",
                )
            try:
                resolved_subject = await self._resolver.resolve_subject(subject)
                resolved_binding = await self._resolver.resolve_binding(binding, offer.subject_id)
                resolved_relationships = await self._resolver.resolve_relationships(
                    relationships,
                    offer.actor_id,
                    offer.subject_id,
                    resolved_binding.binding_id,
                )
            except Exception as exc:
                raise ConsentDeniedError(
                    "evidence_unresolved",
                    "evidence_unresolved: authoritative evidence could not be resolved",
                ) from exc
            if resolved_subject.subject_id != offer.subject_id:
                raise ConsentDeniedError("subject_mismatch", "offer subject does not match proof")
            subject = resolved_subject
            binding = resolved_binding
            relationships = resolved_relationships
            content_hash = _grant_content_hash(
                offer=offer,
                subject=subject,
                actor_kind=actor_kind,
                binding=binding,
                relationships=relationships,
            )
            if idempotency_key is not None:
                existing = await uow.get_idempotency(idempotency_key)
                if existing is not None:
                    result = await _async_replay(uow, existing, content_hash)
                    await uow.commit()
                    return result
            verified_kind = verify_grant(
                offer=offer,
                subject=subject,
                actor_kind=actor_kind,
                binding=binding,
                relationships=relationships,
                now=grant_at,
            )

            current_head = await uow.lock_consent_head(
                offer.actor_id,
                offer.actor_id,
                subject.subject_id,
                binding.binding_id,
                binding.version,
                offer.capability,
                offer.purpose,
            )
            superseded_chain = current_head if current_head is not None else None
            await uow.lock_snapshot_head(
                offer.actor_id,
                subject.subject_id,
                binding.binding_id,
                binding.version,
            )

            consent_id = str(uuid.uuid4())
            snapshot_id = str(uuid.uuid4())
            evidence_id = str(uuid.uuid4())
            new_chain = ConsentEvidence(
                consent_id=consent_id,
                version=(current_head.version + 2 if current_head is not None else 1),
                snapshot_id=snapshot_id,
                status="active",
                subject_id=offer.subject_id,
                resource_owner_id=offer.resource_owner_id,
                actor_id=offer.actor_id,
                actor_kind=verified_kind,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                binding_version=binding.version,
                capability=offer.capability,
                purpose=offer.purpose,
                policy_version=offer.policy_version,
                evidence_id=evidence_id,
                offer_id=offer.offer_id,
                offer_version=offer.version,
                offer_hash=offer.canonical_hash,
                idempotency_key=idempotency_key,
                params=offer.params,
                valid_from=offer.valid_from,
                valid_until=offer.valid_until,
                supersedes_consent_id=(
                    superseded_chain.consent_id if superseded_chain is not None else None
                ),
                superseded_by_consent_id=None,
                canonical_hash="",
            )
            if superseded_chain is not None:
                await uow.append_consent(
                    _superseded_version(superseded_chain, snapshot_id, consent_id)
                )
            await uow.append_consent(new_chain)

            snapshot = await _async_build_snapshot(
                uow,
                subject_id=offer.subject_id,
                binding=binding,
                relationships=relationships,
                policy_version=offer.policy_version,
                created_at=grant_at,
                snapshot_id=snapshot_id,
            )
            await uow.append_snapshot(snapshot)

            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_grant",
                aggregate_id=consent_id,
                version=new_chain.version,
                payload=_outbox_payload(
                    ("actor_id", offer.actor_id),
                    ("binding_id", binding.binding_id),
                    ("binding_version", str(binding.version)),
                    ("capability", offer.capability),
                    ("consent_id", consent_id),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("policy_version", offer.policy_version),
                    ("status", "active"),
                    ("subject_id", offer.subject_id),
                    ("valid_until", offer.valid_until.isoformat()),
                ),
                created_at=grant_at,
                idempotency_key=idempotency_key,
            )
            await uow.append_outbox(event)
            audit = AuditEntry(
                audit_id=str(uuid.uuid4()),
                event_id=event_id,
                action="grant",
                actor_id=offer.actor_id,
                subject_id=offer.subject_id,
                consent_id=consent_id,
                snapshot_id=snapshot_id,
                payload=_outbox_payload(
                    ("capability", offer.capability),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("policy_version", offer.policy_version),
                ),
                created_at=grant_at,
            )
            await uow.append_audit(audit)
            if idempotency_key is not None:
                await uow.save_idempotency(
                    IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        content_hash=content_hash,
                        consent_id=consent_id,
                        version=new_chain.version,
                        snapshot_id=snapshot_id,
                        event_id=event_id,
                        audit_id=audit.audit_id,
                        subject_id=offer.subject_id,
                        actor_id=offer.actor_id,
                        created_at=grant_at,
                    )
                )
            await uow.commit()
            return ConsentOperationResult(
                evidence=new_chain,
                snapshot=snapshot,
                outbox_event=event,
                audit_entry=audit,
            )
        except BaseException:
            await uow.rollback()
            raise

    async def _terminate(
        self,
        *,
        consent_id: str,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...],
        reason: str,
        operation: Literal["revoke", "dispute"],
        expected_version: int | None,
        idempotency_key: str | None,
        now: datetime | None,
    ) -> ConsentOperationResult:
        acted_at = _utc(now or self._now(), "now")
        content_hash = _termination_content_hash(
            operation=operation,
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            reason=reason,
            relationships=relationships,
        )
        uow = await self._store.transaction()
        try:
            if idempotency_key is not None:
                existing = await uow.get_idempotency(idempotency_key)
                if existing is not None:
                    result = await _async_replay(uow, existing, content_hash)
                    await uow.commit()
                    return result
            latest = await uow.latest_consent(consent_id)
            if latest is None:
                raise ConsentNotFoundError(f"consent {consent_id!r} not found")
            current_head = await uow.lock_consent_head(
                actor_id,
                latest.actor_id,
                latest.subject_id,
                latest.binding_id,
                latest.binding_version,
                latest.capability,
                latest.purpose,
            )
            if current_head is None or current_head.consent_id != latest.consent_id:
                raise ConsentConflictError("consent is no longer the current authority head")
            latest = current_head
            await uow.lock_snapshot_head(
                actor_id,
                latest.subject_id,
                latest.binding_id,
                latest.binding_version,
            )
            if expected_version is not None and latest.version != expected_version:
                raise ConsentConflictError(
                    f"consent {consent_id!r} is at version {latest.version}, not {expected_version}"
                )
            if latest.status != "active":
                raise ConsentConflictError(
                    f"consent {consent_id!r} is {latest.status}; "
                    "only active consents can be revoked or disputed"
                )
            verify_revocation(
                evidence=latest,
                actor_id=actor_id,
                actor_kind=actor_kind,
                relationships=relationships,
                now=acted_at,
            )

            status: str = "revoked" if operation == "revoke" else "disputed"
            snapshot_id = str(uuid.uuid4())
            next_row = ConsentEvidence(
                consent_id=latest.consent_id,
                version=latest.version + 1,
                snapshot_id=snapshot_id,
                status=status,  # type: ignore[arg-type]
                subject_id=latest.subject_id,
                resource_owner_id=latest.resource_owner_id,
                actor_id=latest.actor_id,
                actor_kind=latest.actor_kind,
                device_id=latest.device_id,
                binding_id=latest.binding_id,
                binding_version=latest.binding_version,
                capability=latest.capability,
                purpose=latest.purpose,
                policy_version=latest.policy_version,
                evidence_id=str(uuid.uuid4()),
                offer_id=latest.offer_id,
                offer_version=latest.offer_version,
                offer_hash=latest.offer_hash,
                idempotency_key=idempotency_key,
                params=latest.params,
                valid_from=latest.valid_from,
                valid_until=latest.valid_until,
                supersedes_consent_id=None,
                superseded_by_consent_id=None,
                canonical_hash="",
            )
            await uow.append_consent(next_row)
            previous = await uow.latest_snapshot(
                latest.subject_id, latest.binding_id, latest.binding_version
            )
            if previous is None:
                raise ConsentConsistencyError(
                    f"no snapshot for consent {consent_id!r}; refusing to continue"
                )
            snapshot = await _async_build_snapshot(
                uow,
                subject_id=latest.subject_id,
                binding=previous.binding,
                relationships=previous.relationships,
                policy_version=previous.policy_version,
                created_at=acted_at,
                snapshot_id=snapshot_id,
            )
            await uow.append_snapshot(snapshot)

            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type=("consent_revoke" if operation == "revoke" else "consent_dispute"),
                aggregate_id=consent_id,
                version=next_row.version,
                payload=_outbox_payload(
                    ("actor_id", actor_id),
                    ("binding_id", latest.binding_id),
                    ("consent_id", consent_id),
                    ("reason", reason),
                    ("status", status),
                    ("subject_id", latest.subject_id),
                    ("version", str(next_row.version)),
                ),
                created_at=acted_at,
                idempotency_key=idempotency_key,
            )
            await uow.append_outbox(event)
            audit = AuditEntry(
                audit_id=str(uuid.uuid4()),
                event_id=event_id,
                action=operation,
                actor_id=actor_id,
                subject_id=latest.subject_id,
                consent_id=consent_id,
                snapshot_id=snapshot_id,
                payload=_outbox_payload(("reason", reason)),
                created_at=acted_at,
            )
            await uow.append_audit(audit)
            if idempotency_key is not None:
                await uow.save_idempotency(
                    IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        content_hash=content_hash,
                        consent_id=consent_id,
                        version=next_row.version,
                        snapshot_id=snapshot_id,
                        event_id=event_id,
                        audit_id=audit.audit_id,
                        subject_id=latest.subject_id,
                        actor_id=actor_id,
                        created_at=acted_at,
                    )
                )
            await uow.commit()
            return ConsentOperationResult(
                evidence=next_row,
                snapshot=snapshot,
                outbox_event=event,
                audit_entry=audit,
            )
        except BaseException:
            await uow.rollback()
            raise

    async def revoke(
        self,
        consent_id: str,
        *,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...] = (),
        reason: str = "",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        return await self._terminate(
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            relationships=relationships,
            reason=reason,
            operation="revoke",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    async def dispute(
        self,
        consent_id: str,
        *,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...] = (),
        reason: str = "",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        return await self._terminate(
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            relationships=relationships,
            reason=reason,
            operation="dispute",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    async def expire_due(self, now: datetime | None = None) -> tuple[ConsentOperationResult, ...]:
        acted_at = _utc(now or self._now(), "now")
        uow = await self._store.transaction()
        results: list[ConsentOperationResult] = []
        try:
            for chain in await uow.all_active_chains():
                if chain.valid_until > acted_at:
                    continue
                current_head = await uow.lock_consent_head(
                    "system",
                    chain.actor_id,
                    chain.subject_id,
                    chain.binding_id,
                    chain.binding_version,
                    chain.capability,
                    chain.purpose,
                )
                if current_head is None or current_head.canonical_hash != chain.canonical_hash:
                    continue
                chain = current_head
                await uow.lock_snapshot_head(
                    "system",
                    chain.subject_id,
                    chain.binding_id,
                    chain.binding_version,
                )
                previous = await uow.latest_snapshot(
                    chain.subject_id, chain.binding_id, chain.binding_version
                )
                if previous is None:
                    raise ConsentConsistencyError(
                        f"no snapshot for consent {chain.consent_id!r}; refusing to expire"
                    )
                snapshot_id = str(uuid.uuid4())
                expired_row = ConsentEvidence(
                    consent_id=chain.consent_id,
                    version=chain.version + 1,
                    snapshot_id=snapshot_id,
                    status="expired",
                    subject_id=chain.subject_id,
                    resource_owner_id=chain.resource_owner_id,
                    actor_id=chain.actor_id,
                    actor_kind=chain.actor_kind,
                    device_id=chain.device_id,
                    binding_id=chain.binding_id,
                    binding_version=chain.binding_version,
                    capability=chain.capability,
                    purpose=chain.purpose,
                    policy_version=chain.policy_version,
                    evidence_id=str(uuid.uuid4()),
                    offer_id=chain.offer_id,
                    offer_version=chain.offer_version,
                    offer_hash=chain.offer_hash,
                    idempotency_key=None,
                    params=chain.params,
                    valid_from=chain.valid_from,
                    valid_until=chain.valid_until,
                    supersedes_consent_id=None,
                    superseded_by_consent_id=None,
                    canonical_hash="",
                )
                await uow.append_consent(expired_row)
                snapshot = await _async_build_snapshot(
                    uow,
                    subject_id=chain.subject_id,
                    binding=previous.binding,
                    relationships=previous.relationships,
                    policy_version=previous.policy_version,
                    created_at=acted_at,
                    snapshot_id=snapshot_id,
                )
                await uow.append_snapshot(snapshot)
                event_id = str(uuid.uuid4())
                event = ConsentOutboxEvent(
                    event_id=event_id,
                    aggregate_type="consent_expire",
                    aggregate_id=chain.consent_id,
                    version=expired_row.version,
                    payload=_outbox_payload(
                        ("actor_id", "system"),
                        ("binding_id", chain.binding_id),
                        ("consent_id", chain.consent_id),
                        ("status", "expired"),
                        ("subject_id", chain.subject_id),
                        ("version", str(expired_row.version)),
                    ),
                    created_at=acted_at,
                    idempotency_key=None,
                )
                await uow.append_outbox(event)
                audit = AuditEntry(
                    audit_id=str(uuid.uuid4()),
                    event_id=event_id,
                    action="expire",
                    actor_id="system",
                    subject_id=chain.subject_id,
                    consent_id=chain.consent_id,
                    snapshot_id=snapshot_id,
                    payload=_outbox_payload(("reason", "due")),
                    created_at=acted_at,
                )
                await uow.append_audit(audit)
                results.append(
                    ConsentOperationResult(
                        evidence=expired_row,
                        snapshot=snapshot,
                        outbox_event=event,
                        audit_entry=audit,
                    )
                )
            await uow.commit()
            return tuple(results)
        except BaseException:
            await uow.rollback()
            raise

    async def snapshot(
        self,
        subject_id: str,
        *,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
        policy_version: str | None = None,
        now: datetime | None = None,
    ) -> ConsentSnapshot:
        acted_at = _utc(now or self._now(), "now")
        uow = await self._store.transaction()
        try:
            await uow.lock_snapshot_head(
                "system", subject_id, binding.binding_id, binding.version
            )
            previous = await uow.latest_snapshot(subject_id, binding.binding_id, binding.version)
            resolved_policy = policy_version or (
                previous.policy_version if previous is not None else ""
            )
            if not resolved_policy:
                raise ValueError("policy_version is required for the first snapshot")
            candidate = await _async_build_snapshot(
                uow,
                subject_id=subject_id,
                binding=binding,
                relationships=relationships,
                policy_version=resolved_policy,
                created_at=acted_at,
                snapshot_id=str(uuid.uuid4()),
            )
            if previous is not None and _snapshot_unchanged(previous, candidate):
                await uow.commit()
                return previous
            snapshot = _with_new_identity(
                candidate,
                snapshot_id=str(uuid.uuid4()),
                version=(previous.version + 1 if previous is not None else 1),
                created_at=acted_at,
            )
            await uow.append_snapshot(snapshot)
            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_snapshot",
                aggregate_id=snapshot.snapshot_id,
                version=snapshot.version,
                payload=_outbox_payload(
                    ("actor_id", "system"),
                    ("binding_id", binding.binding_id),
                    ("binding_version", str(binding.version)),
                    ("snapshot_id", snapshot.snapshot_id),
                    ("subject_id", subject_id),
                ),
                created_at=acted_at,
                idempotency_key=None,
            )
            await uow.append_outbox(event)
            await uow.append_audit(
                AuditEntry(
                    audit_id=str(uuid.uuid4()),
                    event_id=event_id,
                    action="snapshot",
                    actor_id="system",
                    subject_id=subject_id,
                    consent_id=None,
                    snapshot_id=snapshot.snapshot_id,
                    payload=_outbox_payload(("version", str(snapshot.version))),
                    created_at=acted_at,
                )
            )
            await uow.commit()
            return snapshot
        except BaseException:
            await uow.rollback()
            raise
