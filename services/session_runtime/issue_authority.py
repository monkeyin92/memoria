"""Profile issue/renew authority seam of ``PostgresSessionRuntimeService``.

``SessionIssueAuthorityMixin`` is composed into ``PostgresSessionRuntimeService``;
the store, policy batch authorizer, receipt repository, signing key, mode
resolver and profile TTL stay owned by the facade and are only declared here
for type checking.  The module exists so profile construction, receipt
persistence and current/active profile loading read on their own, without the
start, subject-switch, renewal and close flows around them.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    CapabilityValue,
    PolicyReceiptV2,
    PurposeValue,
    RuntimeProfileSignedV2,
    RuntimeProfileV2,
    SessionEvent,
    SubjectCategoryValue,
)

from services.policy.context import PolicyContext
from services.policy.production_wiring import ConsentDiscovery
from services.session_runtime.errors import (
    PersistentSessionDenied,
    PersistentSessionExpired,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
)
from services.session_runtime.postgres_authorities import (
    _IssuedProfile,
    _obligation_union,
    _persona_snapshot,
)
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
    SessionRuntimeContext,
)
from services.session_runtime.profile_service import (
    canonical_runtime_decision_purpose,
    sign_runtime_profile_payload,
)
from services.session_runtime.service_mode_resolver import ServiceModeResolver
from services.session_runtime.subject_resolver import (
    DEVICE_BINDING_PRIMARY_REASON,
    SubjectResolution,
)
from services.session_runtime.wire import (
    _digest,
    _LockedBinding,
)

if TYPE_CHECKING:
    from services.policy.engine import PolicyEngine
    from services.policy.session_batch import SessionPolicyBatchAuthorizer
    from services.session_runtime.postgres_authorities import (
        _ActionExecutorReceiptRepository,
    )


class SessionIssueAuthorityMixin:
    """Build, persist and validate one signed Runtime Profile."""

    if TYPE_CHECKING:
        # Shared state owned by the PostgresSessionRuntimeService facade.
        _store: PostgresSessionRuntimeStore
        _policy: PolicyEngine
        _signing_key: bytes
        _profile_ttl: timedelta
        _mode_resolver: ServiceModeResolver
        _receipt_repository: _ActionExecutorReceiptRepository
        _batch: SessionPolicyBatchAuthorizer

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
        ) -> ConsentDiscovery: ...

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
        profile_ttl: timedelta | None = None,
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
                subject_presence=(
                    "device_bound"
                    if resolution.reason_code == DEVICE_BINDING_PRIMARY_REASON
                    and resolution.active_subject_id is not None
                    and resolution.speaker_state == "confirmed"
                    else "resolved"
                ),
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
        persona_assignment_id = binding.persona_for(resolution.active_subject_id)
        persona_id, persona_version = _persona_snapshot(persona_assignment_id)
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
                "persona_assignment_id": persona_assignment_id,
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
                "expires_at": (now + (profile_ttl or self._profile_ttl)).isoformat(),
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

    def _require_profile_signature(
        self,
        profile: RuntimeProfileSignedV2,
    ) -> None:
        """Verify the durable profile payload without imposing its use window."""

        payload = profile.model_dump(mode="json", exclude={"signature"})
        expected = sign_runtime_profile_payload(payload, signing_key=self._signing_key)
        if not hmac.compare_digest(profile.signature, expected):
            raise PersistentSessionDenied("runtime profile signature is invalid")

    def _require_current_profile(
        self,
        profile: RuntimeProfileSignedV2,
        *,
        now: datetime,
    ) -> None:
        self._require_profile_signature(profile)
        if now < profile.issued_at or now >= profile.expires_at:
            raise PersistentSessionExpired("runtime profile is expired")

    async def _load_active_profile_context(
        self,
        connection: asyncpg.Connection,
        *,
        session_id: str,
    ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]:
        """Load one active profile/context pair inside the caller's transaction.

        The projection/epoch consistency checks live here so the close-only path
        and the fenced read path apply exactly the same comparison -- on whatever
        snapshot the caller already holds.
        """
        profile = await self._store.current_profile(
            connection,
            session_id=session_id,
        )
        context = await self._store.current_context(
            connection,
            session_id=session_id,
        )
        if profile is None or context is None:
            raise PersistentSessionNotFound(session_id)
        if profile.runtime_profile_id != context.current_runtime_profile_id:
            raise PersistentSessionUnavailable("current profile projection is inconsistent")
        if profile.session_epoch != context.session_epoch:
            raise PersistentSessionUnavailable("current profile epoch is inconsistent")
        return profile, context

    async def _active_profile_context(
        self,
        *,
        actor_id: str,
        session_id: str,
    ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]:
        """Load one active profile/context pair without applying use-time validity.

        Terminal closure must remain possible after a profile expires: expiry
        denies further conversation work, but it must not strand an active
        authority row forever. Callers still have to verify the stored profile
        signature before using this close-only path.
        """
        try:
            async with self._store.read_transaction(actor_id=actor_id) as connection:
                return await self._load_active_profile_context(
                    connection,
                    session_id=session_id,
                )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
