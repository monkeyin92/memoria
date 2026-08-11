"""Decision calculation followed by durable, retry-idempotent persistence.

The service first computes an in-memory decision, then asks the async repository
to persist its immutable receipt. The caller receives the decision only after
that INSERT succeeds. If insertion fails, ``decide_and_persist`` raises and the
caller receives no allow/deny result. Repository persistence is one transaction
writing one table; there is no multi-table atomic claim and no separate
audit/outbox table. The immutable ``policy_receipts_v2`` row is the audit record.

For a stable scope + idempotency key, retries replay only when every canonical
authoritative input except evaluation time is byte-for-byte equivalent and the
stored evidence remains active. An old allow is never replayed after evidence
invalidation; the current outcome is persisted under a deterministic version
receipt id. Any other authoritative drift fails closed even when effect/reason
are unchanged. Idempotency therefore does not depend on timestamp equality.
Concurrent retries are equivalent to serialized retries: an insert loser reads
the winner's committed receipt and runs the same replay validation.
Every replay must remain inside the stored decision window. Expiry fails closed
and requires a new idempotency key; it never creates another version. Evidence
invalidation alone may create at most eight deterministic receipt generations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyDecision,
    PolicyReceiptV2,
)

from services.policy.context import (
    PolicyContext,
    context_hash,
    effective_action_resource_fence,
)
from services.policy.engine import PolicyEngine
from services.policy.receipts import PolicyReceiptConflictError

MAX_GENERATIONS = 8


class PolicyReceiptRepositoryPort(Protocol):
    """Append-only receipt persistence (INSERT only; no update/delete)."""

    async def insert(self, receipt: PolicyReceiptV2) -> None: ...

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None: ...


@runtime_checkable
class ScopedPolicyReceiptRepositoryPort(Protocol):
    """Optional API-producer seam with independently authenticated scope."""

    async def insert_scoped(
        self,
        receipt: PolicyReceiptV2,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> None: ...

    async def get_scoped(
        self,
        receipt_id: str,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> PolicyReceiptV2 | None: ...


class DecisionService:
    def __init__(
        self,
        *,
        engine: PolicyEngine,
        repository: PolicyReceiptRepositoryPort,
    ) -> None:
        self._engine = engine
        self._repository = repository

    async def decide_and_persist(self, context: PolicyContext) -> PolicyDecision:
        """Compute, then persist both allow and deny receipts before returning.

        Stable-scope retries read before writing. Time-normalized authoritative
        context equality plus active evidence replays the stored receipt;
        invalidated evidence versions the current outcome; any other canonical
        drift raises an immutable conflict. An insertion exception is propagated,
        so the caller never observes an unreceipted decision.
        """
        decision = self._engine.evaluate_bundle(context).decision
        receipt = self._engine.receipt_for(context, decision)
        if context.idempotency_key is None:
            await self._insert(receipt, context)
            return decision

        existing = await self._get(receipt.receipt_id, context)
        if existing is None:
            try:
                await self._insert(receipt, context)
                return decision
            except PolicyReceiptConflictError:
                committed = await self._get(receipt.receipt_id, context)
                if committed is None:
                    raise
                return await self._resolve_replay(
                    existing=committed,
                    fresh_decision=decision,
                    fresh_receipt=receipt,
                    context=context,
                )
        return await self._resolve_replay(
            existing=existing,
            fresh_decision=decision,
            fresh_receipt=receipt,
            context=context,
        )

    async def _resolve_replay(
        self,
        *,
        existing: PolicyReceiptV2,
        fresh_decision: PolicyDecision,
        fresh_receipt: PolicyReceiptV2,
        context: PolicyContext,
    ) -> PolicyDecision:
        if not _stable_scope_matches(existing, context, self._engine.policy_version):
            raise PolicyReceiptConflictError(
                "policy receipt id immutable scope conflict"
            )
        _require_replay_window(existing, context)
        authoritative_match = _replay_authoritative_match(existing, context)
        evidence_valid = _replay_evidence_valid(existing, context)
        if authoritative_match and evidence_valid:
            return _decision_from_receipt(existing)
        if evidence_valid:
            raise PolicyReceiptConflictError(
                "policy receipt immutable authoritative context conflict"
            )

        latest_generation = 0
        latest_version: PolicyReceiptV2 | None = None
        for generation in range(1, MAX_GENERATIONS + 1):
            candidate_id = _versioned_receipt_id(
                context,
                fresh_receipt,
                generation=generation,
            )
            candidate = await self._get(candidate_id, context)
            if candidate is not None:
                latest_generation = generation
                latest_version = candidate

        if latest_version is not None:
            if not _stable_scope_matches(
                latest_version, context, self._engine.policy_version
            ):
                raise PolicyReceiptConflictError(
                    "policy receipt immutable version scope conflict"
                )
            _require_replay_window(latest_version, context)
            if _replay_authoritative_match(latest_version, context) and (
                latest_version.effect == "deny"
                or _replay_evidence_valid(latest_version, context)
            ):
                return _decision_from_receipt(latest_version)
            if latest_generation >= MAX_GENERATIONS:
                raise PolicyReceiptConflictError(
                    "policy receipt generation limit exceeded"
                )

        generation = latest_generation + 1
        version_id = _versioned_receipt_id(
            context,
            fresh_receipt,
            generation=generation,
        )
        version_decision = _decision_with_receipt_id(fresh_decision, version_id)
        version_receipt = _receipt_with_receipt_id(fresh_receipt, version_id)
        try:
            await self._insert(version_receipt, context)
            return version_decision
        except PolicyReceiptConflictError as conflict:
            committed_version = await self._get(version_id, context)
            if committed_version is None:
                raise
            if (
                _stable_scope_matches(
                    committed_version, context, self._engine.policy_version
                )
                and _replay_window_valid(committed_version, context)
                and _replay_authoritative_match(committed_version, context)
                and (
                    committed_version.effect == "deny"
                    or _replay_evidence_valid(committed_version, context)
                )
            ):
                return _decision_from_receipt(committed_version)
            raise PolicyReceiptConflictError(
                "policy receipt immutable concurrent version conflict"
            ) from conflict

    async def _insert(self, receipt: PolicyReceiptV2, context: PolicyContext) -> None:
        scoped = self._repository
        if isinstance(scoped, ScopedPolicyReceiptRepositoryPort):
            await scoped.insert_scoped(
                receipt,
                actor_id=context.actor_id,
                subject_id=context.subject_id,
            )
            return
        await self._repository.insert(receipt)

    async def _get(
        self,
        receipt_id: str,
        context: PolicyContext,
    ) -> PolicyReceiptV2 | None:
        scoped = self._repository
        if isinstance(scoped, ScopedPolicyReceiptRepositoryPort):
            return await scoped.get_scoped(
                receipt_id,
                actor_id=context.actor_id,
                subject_id=context.subject_id,
            )
        return await self._repository.get(receipt_id)


def _stable_scope_matches(
    receipt: PolicyReceiptV2,
    context: PolicyContext,
    policy_version: str,
) -> bool:
    context_action = effective_action_resource_fence(context)
    receipt_action = receipt.action_resource_fence
    if receipt_action is None:
        return False
    return (
        receipt.policy_version,
        receipt.actor_id,
        receipt.subject_id,
        receipt.resource_owner_id,
        receipt.device_id,
        receipt.binding_id,
        receipt.binding_version,
        receipt.session_id,
        receipt.capability,
        receipt.purpose,
        receipt_action.action_resource_id,
        receipt_action.action_revision,
        receipt_action.family_space_id,
        receipt_action.family_owner_subject_id,
        receipt_action.proposal_id,
        receipt_action.proposal_revision,
        receipt_action.voter_subject_id,
    ) == (
        policy_version,
        context.actor_id,
        context.subject_id,
        context.resource_owner_id,
        context.device_id,
        context.binding_id,
        context.binding_version,
        context.session_id,
        context.capability,
        context.purpose,
        context_action.action_resource_id,
        context_action.action_revision,
        context_action.family_space_id,
        context_action.family_owner_subject_id,
        context_action.proposal_id,
        context_action.proposal_revision,
        context_action.voter_subject_id,
    )


def _replay_evidence_valid(
    receipt: PolicyReceiptV2,
    context: PolicyContext,
) -> bool:
    if not receipt.exact_fence:
        return True
    now = context.evaluated_at
    binding = context.binding_evidence
    if (
        binding is None
        or binding.binding_id != receipt.binding_id
        or binding.version != receipt.binding_version
        or binding.canonical_hash != receipt.binding_canonical_hash
        or not binding.is_active_at(now)
    ):
        return False
    current_consents = {
        (evidence.snapshot_id, evidence.revision): evidence
        for evidence in context.consent_snapshot_evidence
    }
    for identity in zip(
        receipt.consent_snapshot_ids,
        receipt.consent_snapshot_revisions,
        strict=True,
    ):
        consent_snapshot = current_consents.get(identity)
        if consent_snapshot is None or not consent_snapshot.is_current_at(now):
            return False
        if not any(
            evidence.is_effective_at(now) and consent_snapshot.contains(evidence)
            for evidence in context.consent_evidence
        ):
            return False
    current_relationships = {
        (evidence.snapshot_id, evidence.revision): evidence
        for evidence in context.relationship_evidence
    }
    for identity in zip(
        receipt.relationship_snapshot_ids,
        receipt.relationship_snapshot_revisions,
        strict=True,
    ):
        relationship_evidence = current_relationships.get(identity)
        if (
            relationship_evidence is None
            or not relationship_evidence.is_active_at(now)
        ):
            return False
    return True


def _replay_authoritative_match(
    existing: PolicyReceiptV2,
    context: PolicyContext,
) -> bool:
    """Compare canonical authority after normalizing retry evaluation time."""
    normalized = replace(context, evaluated_at=existing.created_at)
    return context_hash(normalized) == existing.context_hash


def _replay_window_valid(
    existing: PolicyReceiptV2,
    context: PolicyContext,
) -> bool:
    """Return whether retry evaluation remains in the stored decision window."""
    return existing.created_at <= context.evaluated_at < existing.expires_at


def _require_replay_window(
    existing: PolicyReceiptV2,
    context: PolicyContext,
) -> None:
    if _replay_window_valid(existing, context):
        return
    if context.evaluated_at < existing.created_at:
        raise PolicyReceiptConflictError(
            "policy receipt replay not-yet-valid; use a new idempotency key"
        )
    raise PolicyReceiptConflictError(
        "policy receipt replay expired; use a new idempotency key"
    )


def _decision_from_receipt(receipt: PolicyReceiptV2) -> PolicyDecision:
    return PolicyDecision.model_validate(
        {
            "capability": receipt.capability.value,
            "purpose": receipt.purpose.value,
            "effect": receipt.effect.value,
            "reason_code": receipt.reason_code,
            "obligations": [
                item.model_dump(mode="json") for item in receipt.obligations
            ],
            "policy_version": receipt.policy_version,
            "receipt_id": receipt.receipt_id,
            "context_hash": receipt.context_hash,
            "action_resource_fence": receipt.action_resource_fence.model_dump(
                mode="json"
            ),
            "action_fence_hash": receipt.action_fence_hash,
            "created_at": _rfc3339(receipt.created_at),
            "expires_at": _rfc3339(receipt.expires_at),
        }
    )


def _decision_with_receipt_id(
    decision: PolicyDecision, receipt_id: str
) -> PolicyDecision:
    payload = decision.model_dump(mode="json")
    payload["receipt_id"] = receipt_id
    return PolicyDecision.model_validate(payload)


def _receipt_with_receipt_id(
    receipt: PolicyReceiptV2, receipt_id: str
) -> PolicyReceiptV2:
    payload = receipt.model_dump(mode="json")
    payload["receipt_id"] = receipt_id
    return PolicyReceiptV2.model_validate(payload)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _versioned_receipt_id(
    context: PolicyContext,
    receipt: PolicyReceiptV2,
    *,
    generation: int,
) -> str:
    if generation < 1 or generation > MAX_GENERATIONS:
        raise ValueError("generation is outside the supported range")
    scope_hash = receipt.receipt_id.removeprefix("receipt-").split("-v", 1)[0]
    material = (
        receipt.policy_version,
        context.actor_id,
        context.subject_id,
        context.resource_owner_id,
        context.device_id,
        context.binding_id,
        context.binding_version,
        context.session_id,
        context.capability,
        context.purpose,
        receipt.action_resource_fence.action_resource_id
        if receipt.action_resource_fence is not None
        else None,
        receipt.action_resource_fence.action_revision
        if receipt.action_resource_fence is not None
        else None,
        receipt.action_resource_fence.proposal_id
        if receipt.action_resource_fence is not None
        else None,
        receipt.action_resource_fence.proposal_revision
        if receipt.action_resource_fence is not None
        else None,
        receipt.action_resource_fence.voter_subject_id
        if receipt.action_resource_fence is not None
        else None,
        context.idempotency_key,
        receipt.effect,
        receipt.reason_code,
    )
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    outcome_hash = hashlib.sha256(encoded).hexdigest()
    return f"receipt-{scope_hash}-v{generation}-{outcome_hash}"
