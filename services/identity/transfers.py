"""Ownership-transfer seam of ``IdentityService``.

``IdentityTransferMixin`` is composed into ``IdentityService``; the store and
the transfer authority port stay owned by the facade and are only declared
here for type checking, together with the binding helpers the accept path
calls. The module exists so the two-party transfer lifecycle can be read on
its own, without the person, relationship and binding code around it.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from services.identity.authority import (
    TRANSFER_DEFAULT_TTL,
    TRANSFER_MAX_TTL,
    TransferVerificationError,
)
from services.identity.domain import (
    IdempotencyRecord,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    TransferIntent,
    TransferLifecycleError,
    manifest_from_binding,
    manifest_from_dict,
    transfer_from_dict,
    validate_manifest_wire,
)
from services.identity.repository import AuditEvent, OutboxEvent
from services.identity.service_helpers import (
    _new_id,
    _now,
    _replay_or_conflict,
    _request_hash,
)

if TYPE_CHECKING:
    from services.identity.authority import TransferEvidenceVerifier
    from services.identity.domain import (
        BindingManifest,
        BindingReason,
        BindingRole,
        DeviceBinding,
        DeviceDeclaredMode,
        Permission,
    )
    from services.identity.repository import IdentityStore


class IdentityTransferMixin:
    """Open, accept, cancel and expire two-party ownership transfers."""

    if TYPE_CHECKING:
        # Shared state owned by the IdentityService facade.
        _store: IdentityStore
        _transfer_verifier: TransferEvidenceVerifier

        async def _require_active_binding(
            self,
            device_id: str,
            now: datetime,
            actor_person_id: str | None = None,
        ) -> DeviceBinding: ...

        async def _build_binding(
            self,
            *,
            device_id: str,
            declared_mode: DeviceDeclaredMode,
            account_owner_person_id: str,
            primary_subject_ids: tuple[str, ...],
            roles: tuple[tuple[str, BindingRole], ...],
            role_permissions: dict[tuple[str, BindingRole], frozenset[Permission]]
            | None,
            family_space_id: str | None,
            service_profile_version: str,
            policy_bundle_version: str,
            consent_snapshot_id: str | None,
            persona_assignment_id: str | None,
            reason: BindingReason,
            supersedes_binding_id: str | None,
            valid_from: datetime,
            valid_until: datetime | None,
            now: datetime,
        ) -> DeviceBinding: ...

        def _binding_events(
            self,
            *,
            action: str,
            manifest: BindingManifest,
            actor_person_id: str | None,
            timestamp: datetime,
            extra: dict[str, object] | None = None,
        ) -> tuple[AuditEvent, OutboxEvent]: ...

    # ------------------------------------------------------------------
    # Ownership transfer intents (two-party, step-up + explicit accept)
    # ------------------------------------------------------------------

    async def create_transfer_intent(
        self,
        *,
        device_id: str,
        to_account_owner_person_id: str,
        step_up_evidence_id: str,
        policy_receipt_id: str,
        idempotency_key: str,
        valid_until: datetime | None = None,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> TransferIntent:
        """Open a pending transfer; the current owner must step up and hold a
        verified signed evidence ticket.  A bare ``binding.manage`` call can
        never transfer a device.  ``idempotency_key`` makes network retries
        replay the same intent instead of conflicting."""
        timestamp = _now(now)
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 64:
            raise TransferLifecycleError(
                "idempotency_key must be a bounded non-empty string"
            )
        scope_key = f"{actor_person_id}:{device_id}:transfer.create"
        content_hash = _request_hash(
            device_id=device_id,
            to_account_owner_person_id=to_account_owner_person_id,
            step_up_evidence_id=step_up_evidence_id.strip(),
            policy_receipt_id=policy_receipt_id.strip(),
            valid_until=valid_until.isoformat() if valid_until else None,
        )
        existing = await self._store.get_idempotency_record(
            scope_key, idempotency_key, actor_person_id=actor_person_id
        )
        if existing is not None:
            # Committed replay: identical content replays the immutable
            # result without re-verifying (possibly expired) evidence.
            _replay_or_conflict(existing, content_hash=content_hash)
            return transfer_from_dict(existing.result_payload)
        current = await self._require_active_binding(
            device_id, timestamp, actor_person_id=actor_person_id
        )
        if actor_person_id != current.account_owner_person_id:
            raise IdentityAccessDeniedError(
                f"only account owner {current.account_owner_person_id} may "
                f"create a transfer intent (actor={actor_person_id})"
            )
        step_up = step_up_evidence_id.strip()
        receipt = policy_receipt_id.strip()
        if not step_up or not receipt:
            raise TransferLifecycleError(
                "transfer requires step-up evidence and a policy receipt"
            )
        if not await self._store.person_exists(to_account_owner_person_id):
            raise IdentityNotFoundError(
                f"person {to_account_owner_person_id} does not exist"
            )
        if to_account_owner_person_id == current.account_owner_person_id:
            raise IdentityConflictError("transfer requires a different account owner")
        # Bound the request TTL before consuming any one-time evidence so a
        # malformed TTL never burns a step-up ticket.
        if valid_until is None:
            valid_until = timestamp + TRANSFER_DEFAULT_TTL
        else:
            ttl = valid_until - timestamp
            if ttl <= timedelta(0) or ttl > TRANSFER_MAX_TTL:
                raise TransferLifecycleError(
                    f"transfer valid_until must be within the bounded TTL "
                    f"(<= {TRANSFER_MAX_TTL.days} days from now)"
                )
        # Authority port: the receipt must be a server-signed ticket matching
        # actor/subject/device/binding/version/capability/expiry exactly.
        evidence = await self._transfer_verifier.verify(
            step_up_evidence_id=step_up,
            policy_receipt_id=receipt,
            device_id=device_id,
            binding_id=current.binding_id,
            binding_version=current.binding_version,
            actor_person_id=actor_person_id,
            expected_subject_person_id=to_account_owner_person_id,
            now=timestamp,
        )
        if evidence.subject_person_id != to_account_owner_person_id:
            raise TransferVerificationError(
                "transfer ticket subject does not match the requested target"
            )
        if evidence.binding_id != current.binding_id:
            raise TransferVerificationError(
                "transfer ticket binding does not match the active binding"
            )
        if evidence.binding_version != current.binding_version:
            raise TransferVerificationError(
                "transfer ticket binding version does not match the active binding"
            )
        # The ticket expiry is authoritative; the intent TTL cannot exceed it.
        if evidence.expires_at < (valid_until or timestamp):
            raise TransferVerificationError(
                "transfer ticket expires before the requested intent TTL"
            )
        # Persist only the authority-resolved short evidence id and an
        # irreversible digest of the raw submitted tickets (step-up ticket +
        # policy receipt identity); the raw HMAC ticket never lands in the
        # database.
        evidence_digest = hashlib.sha256(
            f"{receipt}:{step_up}".encode()
        ).hexdigest()
        pending = await self._store.list_transfer_intents(
            device_id,
            statuses=("pending",),
            actor_person_id=actor_person_id,
        )
        if pending:
            raise IdentityConflictError(
                "a transfer intent is already pending for this device"
            )
        intent = TransferIntent(
            transfer_id=_new_id(),
            device_id=device_id,
            from_account_owner_person_id=current.account_owner_person_id,
            to_account_owner_person_id=to_account_owner_person_id,
            status="pending",
            step_up_evidence_id=evidence.step_up_evidence_id,
            policy_receipt_id=evidence.receipt_id,
            idempotency_key=idempotency_key,
            evidence_hash=evidence_digest,
            supersedes_binding_id=current.binding_id,
            created_by_person_id=actor_person_id,
            created_at=timestamp,
            updated_at=timestamp,
            valid_until=valid_until,
        )
        event_id = _new_id()
        record = IdempotencyRecord(
            scope_key=scope_key,
            idempotency_key=idempotency_key,
            operation="transfer.create",
            content_hash=content_hash,
            result_payload=intent.to_dict(),
            created_at=timestamp,
        )
        try:
            await self._store.save_transfer_intent(
                intent,
                audit_event=AuditEvent(
                    event_id=event_id,
                    action="transfer.create",
                    actor_person_id=actor_person_id,
                    subject_person_id=to_account_owner_person_id,
                    person_id=actor_person_id,
                    device_id=device_id,
                    binding_id=current.binding_id,
                    relationship_id=None,
                    payload=intent.to_dict(),
                    created_at=timestamp,
                ),
                outbox_event=OutboxEvent(
                    outbox_id=_new_id(),
                    event_id=event_id,
                    topic="identity.transfer.created",
                    payload=intent.to_dict(),
                    created_at=timestamp,
                ),
                idempotency_record=record,
                actor_person_id=actor_person_id,
            )
        except IdentityConflictError as exc:
            if "already used" not in str(exc):
                raise
            winner = await self._store.get_idempotency_record(
                scope_key, idempotency_key, actor_person_id=actor_person_id
            )
            if winner is None:
                raise exc from exc
            if winner.operation != "transfer.create":
                raise IdentityConflictError(
                    "idempotency key already used by a different operation"
                ) from exc
            _replay_or_conflict(winner, content_hash=content_hash)
            return transfer_from_dict(winner.result_payload)
        return intent

    async def accept_transfer_intent(
        self,
        *,
        transfer_id: str,
        actor_person_id: str,
        declared_mode: DeviceDeclaredMode,
        primary_subject_ids: tuple[str, ...],
        service_profile_version: str | None = None,
        policy_bundle_version: str | None = None,
        roles: tuple[tuple[str, BindingRole], ...] | None = None,
        role_permissions: dict[tuple[str, BindingRole], frozenset[Permission]]
        | None = None,
        family_space_id: str | None = None,
        consent_snapshot_id: str | None = None,
        persona_assignment_id: str | None = None,
        idempotency_key: str,
        valid_until: datetime | None = None,
        now: datetime | None = None,
    ) -> BindingManifest:
        """The target owner accepts: single transaction issues the next
        binding version, invalidates the old one and inherits no roles."""
        timestamp = _now(now)
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 64:
            raise TransferLifecycleError(
                "idempotency_key must be a bounded non-empty string"
            )
        scope_key = f"transfer:{transfer_id}:accept"
        content_hash = _request_hash(
            actor_person_id=actor_person_id,
            declared_mode=declared_mode,
            primary_subject_ids=primary_subject_ids,
            roles=roles or (),
            role_permissions={
                f"{key[0]}:{key[1]}": sorted(value)
                for key, value in (role_permissions or {}).items()
            },
            family_space_id=family_space_id,
            valid_until=valid_until.isoformat() if valid_until else None,
        )
        existing = await self._store.get_idempotency_record(
            scope_key, idempotency_key, actor_person_id=actor_person_id
        )
        if existing is not None:
            _replay_or_conflict(existing, content_hash=content_hash)
            return manifest_from_dict(existing.result_payload)
        intent = await self._store.get_transfer_intent(
            transfer_id, actor_person_id=actor_person_id
        )
        if intent is None:
            raise IdentityNotFoundError(f"transfer {transfer_id} does not exist")
        if intent.status == "accepted" and intent.resulting_binding_id is not None:
            if actor_person_id != intent.to_account_owner_person_id:
                raise IdentityAccessDeniedError(
                    f"person {actor_person_id} is not the transfer target"
                )
            persisted = await self._store.get_binding(
                intent.resulting_binding_id, actor_person_id=actor_person_id
            )
            if persisted is None:
                raise IdentityNotFoundError(
                    f"accepted transfer result {intent.resulting_binding_id} "
                    "does not exist"
                )
            manifest = manifest_from_binding(persisted)
            validate_manifest_wire(manifest)
            raise IdentityConflictError(
                "transfer is already accepted; replay requires the original "
                "idempotency key and content"
            )
        if actor_person_id != intent.to_account_owner_person_id:
            raise IdentityAccessDeniedError(
                f"only target owner {intent.to_account_owner_person_id} may "
                f"accept transfer {transfer_id}"
            )
        if intent.status == "cancelled":
            raise IdentityConflictError("transfer was cancelled")
        if intent.status == "conflicted":
            raise IdentityConflictError("transfer conflicts with a binding change")
        if intent.status != "pending":
            raise IdentityConflictError(
                f"transfer cannot be accepted (status={intent.status})"
            )
        if intent.valid_until is not None and intent.valid_until <= timestamp:
            expired = replace(intent, status="expired", updated_at=timestamp)
            await self._store.save_transfer_intent(
                expired,
                audit_event=AuditEvent(
                    event_id=_new_id(),
                    action="transfer.expire",
                    actor_person_id=None,
                    subject_person_id=intent.to_account_owner_person_id,
                    person_id=intent.from_account_owner_person_id,
                    device_id=intent.device_id,
                    binding_id=intent.supersedes_binding_id,
                    relationship_id=None,
                    payload=expired.to_dict(),
                    created_at=timestamp,
                ),
                expected_updated_at=intent.updated_at,
                actor_person_id=actor_person_id,
            )
            raise IdentityConflictError(
                f"transfer {transfer_id} expired at {intent.valid_until.isoformat()}"
            )
        current = await self._store.get_active_binding(
            intent.device_id, timestamp, actor_person_id=actor_person_id
        )
        if current is None or current.binding_id != intent.supersedes_binding_id:
            conflicted = replace(intent, status="conflicted", updated_at=timestamp)
            await self._store.save_transfer_intent(
                conflicted,
                audit_event=AuditEvent(
                    event_id=_new_id(),
                    action="transfer.conflict",
                    actor_person_id=actor_person_id,
                    subject_person_id=intent.to_account_owner_person_id,
                    person_id=intent.from_account_owner_person_id,
                    device_id=intent.device_id,
                    binding_id=intent.supersedes_binding_id,
                    relationship_id=None,
                    payload=conflicted.to_dict(),
                    created_at=timestamp,
                ),
                expected_updated_at=intent.updated_at,
                actor_person_id=actor_person_id,
            )
            raise IdentityConflictError(
                "the device binding changed after the transfer intent was created"
            )
        binding = await self._build_binding(
            device_id=intent.device_id,
            declared_mode=declared_mode,
            account_owner_person_id=intent.to_account_owner_person_id,
            primary_subject_ids=primary_subject_ids,
            roles=roles or (),
            role_permissions=role_permissions,
            family_space_id=family_space_id,
            service_profile_version=(
                service_profile_version or current.service_profile_version
            ),
            policy_bundle_version=(
                policy_bundle_version or current.policy_bundle_version
            ),
            consent_snapshot_id=current.consent_snapshot_id,
            persona_assignment_id=(
                persona_assignment_id or current.persona_assignment_id
            ),
            reason="transfer",
            supersedes_binding_id=current.binding_id,
            valid_from=timestamp,
            valid_until=valid_until,
            now=timestamp,
        )
        accepted = replace(
            intent,
            status="accepted",
            resulting_binding_id=binding.binding_id,
            accepted_at=timestamp,
            updated_at=timestamp,
        )
        transfer_manifest = manifest_from_binding(binding)
        accept_event_id = _new_id()
        accept_audit = AuditEvent(
            event_id=accept_event_id,
            action="transfer.accept",
            actor_person_id=actor_person_id,
            subject_person_id=intent.to_account_owner_person_id,
            person_id=intent.from_account_owner_person_id,
            device_id=intent.device_id,
            binding_id=current.binding_id,
            relationship_id=None,
            payload=accepted.to_dict(),
            created_at=timestamp,
        )
        binding_audit, binding_outbox = self._binding_events(
            action="binding.transfer",
            manifest=transfer_manifest,
            actor_person_id=actor_person_id,
            timestamp=timestamp,
        )
        accept_outbox = OutboxEvent(
            outbox_id=_new_id(),
            event_id=accept_event_id,
            topic="identity.transfer.accepted",
            payload=accepted.to_dict(),
            created_at=timestamp,
        )
        try:
            persisted = await self._store.complete_transfer(
                intent=accepted,
                binding=binding,
                previous_binding_id=current.binding_id,
                audit_events=(accept_audit, binding_audit),
                outbox_events=(accept_outbox, binding_outbox),
                idempotency_record=IdempotencyRecord(
                    scope_key=scope_key,
                    idempotency_key=idempotency_key,
                    operation="transfer.accept",
                    content_hash=content_hash,
                    result_payload=transfer_manifest.to_dict(),
                    created_at=timestamp,
                ),
                actor_person_id=actor_person_id,
            )
        except IdentityConflictError as exc:
            if "already used" not in str(exc):
                raise
            winner = await self._store.get_idempotency_record(
                scope_key, idempotency_key, actor_person_id=actor_person_id
            )
            if winner is None:
                raise exc from exc
            if winner.operation != "transfer.accept":
                raise IdentityConflictError(
                    "idempotency key already used by a different operation"
                ) from exc
            _replay_or_conflict(winner, content_hash=content_hash)
            return manifest_from_dict(winner.result_payload)
        manifest = manifest_from_binding(persisted)
        validate_manifest_wire(manifest)
        return manifest

    async def cancel_transfer_intent(
        self,
        *,
        transfer_id: str,
        actor_person_id: str,
        idempotency_key: str,
        reason: str = "cancelled by owner",
        now: datetime | None = None,
    ) -> TransferIntent:
        timestamp = _now(now)
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 64:
            raise TransferLifecycleError(
                "idempotency_key must be a bounded non-empty string"
            )
        scope_key = f"transfer:{transfer_id}:cancel"
        content_hash = _request_hash(
            actor_person_id=actor_person_id,
            reason=reason.strip(),
        )
        existing = await self._store.get_idempotency_record(
            scope_key, idempotency_key, actor_person_id=actor_person_id
        )
        if existing is not None:
            _replay_or_conflict(existing, content_hash=content_hash)
            return transfer_from_dict(existing.result_payload)
        intent = await self._store.get_transfer_intent(
            transfer_id, actor_person_id=actor_person_id
        )
        if intent is None:
            raise IdentityNotFoundError(f"transfer {transfer_id} does not exist")
        if actor_person_id != intent.from_account_owner_person_id:
            raise IdentityAccessDeniedError(
                f"only the current owner {intent.from_account_owner_person_id} "
                f"may cancel transfer {transfer_id}"
            )
        if intent.status == "cancelled":
            raise IdentityConflictError(
                "transfer is already cancelled; replay requires the original "
                "idempotency key and content"
            )
        if intent.status != "pending":
            raise IdentityConflictError(
                f"only pending transfers can be cancelled (status={intent.status})"
            )
        cancelled = replace(
            intent,
            status="cancelled",
            cancelled_at=timestamp,
            cancelled_by_person_id=actor_person_id,
            cancel_reason=reason.strip()[:256],
            updated_at=timestamp,
        )
        cancel_event_id = _new_id()
        try:
            await self._store.save_transfer_intent(
                cancelled,
                audit_event=AuditEvent(
                    event_id=cancel_event_id,
                    action="transfer.cancel",
                    actor_person_id=actor_person_id,
                    subject_person_id=intent.to_account_owner_person_id,
                    person_id=actor_person_id,
                    device_id=intent.device_id,
                    binding_id=intent.supersedes_binding_id,
                    relationship_id=None,
                    payload=cancelled.to_dict(),
                    created_at=timestamp,
                ),
                outbox_event=OutboxEvent(
                    outbox_id=_new_id(),
                    event_id=cancel_event_id,
                    topic="identity.transfer.cancelled",
                    payload=cancelled.to_dict(),
                    created_at=timestamp,
                ),
                idempotency_record=IdempotencyRecord(
                    scope_key=scope_key,
                    idempotency_key=idempotency_key,
                    operation="transfer.cancel",
                    content_hash=content_hash,
                    result_payload=cancelled.to_dict(),
                    created_at=timestamp,
                ),
                expected_updated_at=intent.updated_at,
                actor_person_id=actor_person_id,
            )
        except IdentityConflictError as exc:
            if "already used" not in str(exc):
                raise
            winner = await self._store.get_idempotency_record(
                scope_key, idempotency_key, actor_person_id=actor_person_id
            )
            if winner is None:
                raise exc from exc
            if winner.operation != "transfer.cancel":
                raise IdentityConflictError(
                    "idempotency key already used by a different operation"
                ) from exc
            _replay_or_conflict(winner, content_hash=content_hash)
            return transfer_from_dict(winner.result_payload)
        return cancelled

    async def expire_transfer_intents(self, now: datetime | None = None) -> int:
        timestamp = _now(now)
        count = 0
        for intent in await self._store.scan_transfer_intents(
            statuses=("pending",), scope="migration"
        ):
            if intent.valid_until is not None and intent.valid_until <= timestamp:
                expired = replace(intent, status="expired", updated_at=timestamp)
                await self._store.save_transfer_intent(
                    expired,
                    audit_event=AuditEvent(
                        event_id=_new_id(),
                        action="transfer.expire",
                        actor_person_id=None,
                        subject_person_id=intent.to_account_owner_person_id,
                        person_id=intent.from_account_owner_person_id,
                        device_id=intent.device_id,
                        binding_id=intent.supersedes_binding_id,
                        relationship_id=None,
                        payload=expired.to_dict(),
                        created_at=timestamp,
                    ),
                    expected_updated_at=intent.updated_at,
                    actor_person_id=None,
                    scope="migration",
                )
                count += 1
        return count

    async def get_transfer_intent(
        self, transfer_id: str, actor_person_id: str | None = None
    ) -> TransferIntent:
        intent = await self._store.get_transfer_intent(
            transfer_id, actor_person_id=actor_person_id
        )
        if intent is None:
            raise IdentityNotFoundError(f"transfer {transfer_id} does not exist")
        return intent

    async def list_transfer_intents(
        self,
        device_id: str,
        statuses: tuple[str, ...] | None = None,
        actor_person_id: str | None = None,
    ) -> tuple[TransferIntent, ...]:
        return await self._store.list_transfer_intents(
            device_id,
            statuses=statuses,
            actor_person_id=actor_person_id,
        )
