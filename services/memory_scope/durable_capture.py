"""Durable capture seam of ``MemoryScopeService``.

``DurableCaptureMixin`` is composed into ``MemoryScopeService``; the store,
resolver, receipt verifier and consent verifier stay owned by the facade and
are only declared here for type checking.  The module exists so the single
fail-closed durable write path can be read on its own, without the family
proposal, promotion, read and retention code around it.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from packages.contracts.generated.python.multi_subject_contracts import MemoryScope

from services.memory_scope.domain import (
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryWriteDraft,
    MemoryWriteRejected,
    PolicyEffect,
    ResolutionContext,
    RetentionPolicy,
    WriteFenceMissingError,
    ensure_sensitive_source,
)

if TYPE_CHECKING:
    from services.memory_scope.domain import PolicyObligation, WriteFence
    from services.memory_scope.repository import MemoryStore
    from services.memory_scope.resolver import MemoryScopeResolver
    from services.policy.receipts import PolicyReceiptV2


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class DurableCaptureMixin:
    """Persist one durable memory record, fail closed."""

    if TYPE_CHECKING:
        # Shared state and helpers owned by the MemoryScopeService facade.
        _store: MemoryStore
        _resolver: MemoryScopeResolver

        @staticmethod
        def _authoritative_obligation_codes(
            receipt: PolicyReceiptV2,
        ) -> tuple[PolicyObligation, ...]: ...

        def _enforce_write_fence(
            self,
            context: ResolutionContext,
            *,
            actor_subject_id: str,
            now: datetime,
        ) -> WriteFence: ...

        def _enforce_actor_binding_role(
            self,
            *,
            actor_subject_id: str,
            subject_id: str,
            requested_scope: MemoryScope,
            fence: WriteFence,
        ) -> None: ...

        def _retention_policy(
            self,
            receipt: PolicyReceiptV2,
            *,
            now: datetime,
        ) -> tuple[str, datetime | None]: ...

        async def _verify_consent_authoritative(
            self,
            *,
            snapshot_id: str,
            scope: str,
            covers_subjects: tuple[str, ...],
            now: datetime,
        ) -> None: ...

        async def _verify_receipt(
            self,
            *,
            context: ResolutionContext | None,
            fence: WriteFence,
            actor_subject_id: str,
            capability: str,
            now: datetime,
            receipt_id: str | None = None,
        ) -> PolicyReceiptV2 | None: ...

    async def capture(
        self,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        *,
        actor_subject_id: str,
    ) -> MemoryRecord:
        """Resolve and persist one durable memory record, fail closed."""

        now = _now()
        fence = self._enforce_write_fence(
            context, actor_subject_id=actor_subject_id, now=now
        )

        subject = context.subject
        subject_id = subject.active_subject_id
        if subject_id is None:
            raise MemoryWriteRejected("subject_id missing")
        if context.policy.receipt_id is None:
            raise MemoryWriteRejected("policy receipt missing")
        if context.consent.snapshot_id is None:
            raise MemoryWriteRejected("consent snapshot missing")
        ensure_sensitive_source(
            subject_id=subject_id,
            resource_owner_id=subject_id,
            source_evidence_ids=draft.source_evidence_ids,
            policy_receipt_id=context.policy.receipt_id,
            consent_snapshot_id=context.consent.snapshot_id,
        )

        # P0 contract: the verified receipt is the ONLY authority for the
        # decision (effect/obligations/purpose) - the caller's policy input
        # is a routing hint only and is replaced before resolution.
        receipt = await self._verify_receipt(
            context=context,
            fence=fence,
            actor_subject_id=actor_subject_id,
            capability="memory_capture",
            now=now,
        )
        if receipt is None:
            raise MemoryWriteRejected("policy receipt not verified")
        if context.consent.snapshot_id not in receipt.consent_snapshot_ids:
            raise MemoryWriteRejected(
                "consent snapshot was not referenced by the verified policy receipt"
            )
        authoritative = replace(
            context,
            policy=replace(
                context.policy,
                effect=PolicyEffect.from_value(receipt.effect)
                or PolicyEffect.POLICY_EFFECT_DENY,
                obligations=self._authoritative_obligation_codes(receipt),
                receipt_id=receipt.receipt_id,
            ),
        )
        resolution = self._resolver.resolve(authoritative)
        if not resolution.persistable:
            raise MemoryWriteRejected(
                f"memory write rejected: {resolution.reason_code}"
            )
        if context.requested_scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
            raise MemoryWriteRejected(
                "family_shared writes must go through propose_shared +"
                " per-co-subject confirmation"
            )
        if (
            context.requested_scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
            and "raw_transcript" in draft.payload
        ):
            raise MemoryWriteRejected(
                "guardian_summary payload must not contain raw transcripts"
            )
        # Fourth review: the consent snapshot is verified against the
        # authoritative consent service; the caller's cover tuple is never
        # trusted on its own.
        await self._verify_consent_authoritative(
            snapshot_id=context.consent.snapshot_id,
            scope="memory",
            covers_subjects=(subject_id,),
            now=now,
        )

        if fence is None:  # pragma: no cover - resolver already denied
            raise WriteFenceMissingError("write fence required for durable capture")
        self._enforce_actor_binding_role(
            actor_subject_id=actor_subject_id,
            subject_id=subject_id,
            requested_scope=context.requested_scope,
            fence=fence,
        )

        retention, retention_expires_at = self._retention_policy(
            receipt, now=now
        )
        record_id = _new_id()
        record = MemoryRecord(
            record_id=record_id,
            scope=cast(MemoryScope, context.requested_scope),
            subject_id=subject_id,
            resource_owner_id=subject_id,
            family_space_id=subject.family_space_id,
            source_evidence_ids=draft.source_evidence_ids,
            policy_receipt_id=context.policy.receipt_id,
            consent_snapshot_id=context.consent.snapshot_id,
            memory_type=draft.memory_type,
            confidence=draft.confidence,
            status="confirmed",
            retention=cast(RetentionPolicy, retention),
            retention_expires_at=retention_expires_at,
            payload={**draft.payload, "content": draft.content},
            created_by_actor_id=actor_subject_id,
            created_at=now,
        )
        status_event = MemoryRecordStatusEvent(
            event_id=_new_id(),
            record_id=record_id,
            status="confirmed",
            reason_code="captured",
            created_at=now,
        )
        await self._store.persist_record(
            record,
            actor_family_space_id=fence.family_space_id,
            status_events=(status_event,),
            outbox=(
                MemoryOutboxEvent(
                    outbox_id=_new_id(),
                    event_id=f"{record_id}:captured",
                    topic="memory.record.captured",
                    payload={
                        "record_id": record_id,
                        "scope": record.scope.value,
                        "subject_id": subject_id,
                        "policy_receipt_id": record.policy_receipt_id,
                    },
                    created_at=now,
                ),
            ),
            audit=(
                MemoryAuditEvent(
                    event_id=_new_id(),
                    action="memory.capture",
                    actor_subject_id=actor_subject_id,
                    subject_id=subject_id,
                    record_id=record_id,
                    proposal_id=None,
                    payload={"scope": record.scope.value, "reason": resolution.reason_code},
                    created_at=now,
                ),
            ),
        )
        return record
