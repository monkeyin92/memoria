"""Action authorization seam of ``PostgresSessionRuntimeService``.

``SessionActionAuthorityMixin`` is composed into ``PostgresSessionRuntimeService``;
the store, policy, identity, device and batch authorizer stay owned by the
facade and are only declared here for type checking.  The module exists so the
deferred-action path (fence classification, replay receipts, terminal receipt
locking and the one Policy receipt per action) reads on its own, without the
profile, subject, renewal and close flows around it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
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
from services.policy.evidence import ConsentEvidencePort, ConsentSnapshotEvidencePort
from services.policy.receipts import (
    PolicyReceiptConflictError,
    exact_evidence_fence_valid,
)
from services.session_runtime.device_trust import DeviceTrustSnapshot
from services.session_runtime.errors import (
    PersistentSessionDenied,
    PersistentSessionUnavailable,
)
from services.session_runtime.postgres_store import (
    SessionRuntimeAuthorityUnavailable,
    SessionRuntimeConflict,
    SessionRuntimeContext,
)
from services.session_runtime.profile_service import (
    PROFILE_ISSUE_DEFERRED_CAPABILITIES,
    canonical_runtime_decision_purpose,
)
from services.session_runtime.wire import (
    _digest,
    _LockedBinding,
    _subject_presence_from_profile,
)

if TYPE_CHECKING:
    from services.policy.engine import PolicyEngine
    from services.policy.production_wiring import (
        PostgresCurrentConsentAuthorityAdapter,
    )
    from services.policy.session_batch import SessionPolicyBatchAuthorizer
    from services.session_runtime.postgres_authorities import (
        _ActionExecutorReceiptRepository,
        _PostgresDeviceAuthority,
        _PostgresIdentityAuthority,
    )
    from services.session_runtime.postgres_store import PostgresSessionRuntimeStore


class SessionActionAuthorityMixin:
    """Authorize one deferred action at an exact Session fence."""

    if TYPE_CHECKING:
        # Shared state owned by the PostgresSessionRuntimeService facade.
        _store: PostgresSessionRuntimeStore
        _policy: PolicyEngine
        _identity: _PostgresIdentityAuthority
        _device: _PostgresDeviceAuthority
        _receipt_repository: _ActionExecutorReceiptRepository
        _consent: PostgresCurrentConsentAuthorityAdapter
        _batch: SessionPolicyBatchAuthorizer

        async def current(
            self,
            *,
            actor_id: str,
            session_id: str,
            now: datetime,
        ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]: ...

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
        trust: DeviceTrustSnapshot,
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
            subject_presence=_subject_presence_from_profile(profile, actor_id=actor_id),
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
        trust: DeviceTrustSnapshot,
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
