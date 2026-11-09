"""Relationship-lifecycle seam of ``IdentityService``.

``IdentityRelationshipMixin`` is composed into ``IdentityService``; the store
handle and the person read stay owned by the facade and are only declared here
for type checking. The module exists so propose / attest / confirm / dispute
and the fail-closed delegation cascade can be read on their own, without the
person, binding and transfer code around them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from services.identity.domain import (
    ALL_PERMISSIONS,
    MAX_DELEGATION_DEPTH,
    AgeEvidenceError,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    Relationship,
    RelationshipLifecycleError,
    RoleConstraintError,
)
from services.identity.repository import AuditEvent, OutboxEvent
from services.identity.service_helpers import _new_id, _now

if TYPE_CHECKING:
    from services.identity.domain import (
        Permission,
        PersonSubject,
        RelationshipStatus,
        RelationType,
    )
    from services.identity.repository import IdentityStore


#: Evidence ids for a relationship the binding owner attested at binding time
#: for a person with no account (see ``attest_binding_relationship``).
BINDING_ATTESTATION_EVIDENCE: dict[str, str] = {
    "guardian_of": "guardian_attestation_v1:device_binding",
    "delegate_for": "delegate_attestation_v1:device_binding",
}


#: One-sided links (relation, status, evidence) that make someone a crisis contact.
_CRISIS_CONTACT_LINKS: dict[str, frozenset[tuple[str, str, str | None]]] = {
    "guardian_of": frozenset({
        ("guardian_of", "pending", "guardian_declaration_v1:device_binding"),
        ("guardian_of", "active", BINDING_ATTESTATION_EVIDENCE["guardian_of"]),
    }),
    "delegate_for": frozenset({("delegate_for", "active", BINDING_ATTESTATION_EVIDENCE["delegate_for"])}),
}

#: What a redacted person is shown as (``redact_bound_subject``).
REDACTED_DISPLAY_NAME = "已删除的使用人"


class IdentityRelationshipMixin:
    """Run the relationship lifecycle and its fail-closed delegation cascade."""

    if TYPE_CHECKING:
        # Shared state owned by the IdentityService facade.
        _store: IdentityStore

        async def get_person(
            self, person_id: str, actor_person_id: str | None = None
        ) -> PersonSubject: ...

    async def propose_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: RelationType,
        established_evidence_id: str,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        permissions: frozenset[Permission] = frozenset(),
        requires_confirmation: bool = True,
        can_delegate: bool = False,
        delegated_from_relationship_id: str | None = None,
        relationship_id: str | None = None,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        """Create a pending relationship; both parties must confirm to activate.

        The actor must be the authenticated source person (no third party may
        propose on someone else's behalf), delegation grants are a subset of
        the parent's grants, and non-self relationships always require
        two-party confirmation (acceptance 2/3).
        """
        timestamp = _now(now)
        if actor_person_id != source_person_id:
            raise IdentityAccessDeniedError(
                f"person {actor_person_id} may not propose a relationship "
                f"for source {source_person_id}"
            )
        for endpoint in (source_person_id, target_person_id):
            if not await self._store.person_exists(endpoint):
                raise IdentityNotFoundError(f"person {endpoint} does not exist")
        unknown = permissions - ALL_PERMISSIONS
        if unknown:
            raise RoleConstraintError(f"unknown permissions {sorted(unknown)!r}")
        delegation_depth = 0
        if delegated_from_relationship_id is not None:
            parent = await self._store.get_relationship(
                delegated_from_relationship_id
            )
            if parent is None:
                raise IdentityNotFoundError(
                    f"delegation source relationship {delegated_from_relationship_id} "
                    "does not exist"
                )
            if not parent.can_delegate or parent.status != "active":
                raise RelationshipLifecycleError(
                    "delegation requires an active delegatable source relationship"
                )
            if parent.target_person_id != source_person_id:
                raise RelationshipLifecycleError(
                    "delegation chains require the child source to equal the "
                    "parent target (source/target alignment)"
                )
            if not permissions <= parent.permissions:
                raise RelationshipLifecycleError(
                    "delegation permissions must be a subset of the parent "
                    "relationship's permissions"
                )
            if parent.delegation_depth + 1 > MAX_DELEGATION_DEPTH:
                raise RelationshipLifecycleError(
                    f"delegation depth exceeds {MAX_DELEGATION_DEPTH}"
                )
            delegation_depth = parent.delegation_depth + 1
        if relation_type != "self" and not requires_confirmation:
            raise RelationshipLifecycleError(
                "non-self relationships must require two-party confirmation"
            )
        auto_active = relation_type == "self"
        relationship = Relationship(
            relationship_id=relationship_id or _new_id(),
            source_person_id=source_person_id,
            target_person_id=target_person_id,
            relation_type=relation_type,
            status="active" if auto_active else "pending",
            valid_from=valid_from or timestamp,
            valid_until=valid_until,
            established_evidence_id=established_evidence_id,
            confirmed_by_source_at=timestamp if auto_active else None,
            confirmed_by_target_at=timestamp if relation_type == "self" else None,
            requires_confirmation=requires_confirmation,
            can_delegate=can_delegate,
            delegated_from_relationship_id=delegated_from_relationship_id,
            delegation_depth=delegation_depth,
            permissions=permissions,
            created_at=timestamp,
            updated_at=timestamp,
        )
        await self._store.save_relationship(
            relationship,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="relationship.propose",
                actor_person_id=actor_person_id,
                subject_person_id=target_person_id,
                person_id=source_person_id,
                device_id=None,
                binding_id=None,
                relationship_id=relationship.relationship_id,
                payload=relationship.to_dict(),
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
        )
        return relationship

    async def attest_binding_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: Literal["guardian_of", "delegate_for"],
        actor_person_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        """Activate the binding owner's own attestation for a person without an account.

        Product decision (2026-09-25): a device serves one person. When a
        phone-verified adult binds a device for someone who has no account of
        their own (a child, or an elderly parent) and declares the
        relationship, that declaration is the relationship. The target cannot
        confirm an endpoint, so it activates on the source side alone and
        records ``BINDING_ATTESTATION_EVIDENCE[relation_type]``: never a
        two-party confirmation, a WeChat verification or a manual review.
        Callers must only use this for a target they just registered in the
        same binding request.
        """
        timestamp = _now(now)
        if actor_person_id != source_person_id:
            raise IdentityAccessDeniedError(
                f"person {actor_person_id} may not attest a relationship "
                f"for source {source_person_id}"
            )
        evidence_id = BINDING_ATTESTATION_EVIDENCE.get(relation_type)
        if evidence_id is None:
            raise RelationshipLifecycleError(
                f"{relation_type} cannot be attested by a binding owner"
            )
        if source_person_id == target_person_id:
            raise RelationshipLifecycleError("an attested relationship needs two people")
        source = await self.get_person(source_person_id, actor_person_id=actor_person_id)
        if source.subject_category != "adult" or source.age_evidence_status != "verified":
            raise AgeEvidenceError("only a verified adult may attest a relationship")
        if not await self._store.person_exists(target_person_id):
            raise IdentityNotFoundError(f"person {target_person_id} does not exist")
        relationship = Relationship(
            relationship_id=_new_id(),
            source_person_id=source_person_id,
            target_person_id=target_person_id,
            relation_type=relation_type,
            status="active",
            valid_from=timestamp,
            valid_until=None,
            established_evidence_id=evidence_id,
            confirmed_by_source_at=timestamp,
            confirmed_by_target_at=None,
            requires_confirmation=True,
            can_delegate=False,
            delegated_from_relationship_id=None,
            delegation_depth=0,
            permissions=frozenset(),
            created_at=timestamp,
            updated_at=timestamp,
        )
        await self._store.save_relationship(
            relationship,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="relationship.attest",
                actor_person_id=actor_person_id,
                subject_person_id=target_person_id,
                person_id=source_person_id,
                device_id=None,
                binding_id=None,
                relationship_id=relationship.relationship_id,
                payload=relationship.to_dict(),
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
        )
        return relationship

    async def redact_bound_subject(
        self,
        *,
        person_id: str,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> PersonSubject:
        """Clear what identifies a child once their binding is gone.

        Only the binding owner who attested this person (guardian_of or
        delegate_for) may, and only after no binding serves them any more:
        the name is still needed while a device does. The person becomes a
        disabled placeholder with the domain's unknown age band, and earlier
        audit copies of the name are scrubbed; relationships and bindings stay
        as content-free audit. Adults and account holders are never cleared.
        """
        timestamp = _now(now)
        person = await self.get_person(person_id, actor_person_id=actor_person_id)
        if (
            person.status == "disabled"
            and person.display_name == REDACTED_DISPLAY_NAME
            and person.subject_category == "unknown"
        ):
            return person
        if person.subject_category == "adult":
            raise IdentityAccessDeniedError(
                "an adult person is not cleared by an unbind"
            )
        relationships = await self._store.list_relationships(
            person_id, statuses=("active", "pending"), actor_person_id=actor_person_id
        )
        attested = {
            BINDING_ATTESTATION_EVIDENCE["guardian_of"],
            BINDING_ATTESTATION_EVIDENCE["delegate_for"],
            "guardian_declaration_v1:device_binding",
        }
        if not any(
            item.source_person_id == actor_person_id
            and item.target_person_id == person_id
            and item.relation_type in {"guardian_of", "delegate_for"}
            and item.established_evidence_id in attested
            for item in relationships
        ):
            raise IdentityAccessDeniedError(
                "only the owner who attested this person may redact them"
            )
        if await self._store.list_active_bindings_for_person(
            person_id, timestamp, actor_person_id=actor_person_id
        ):
            raise RelationshipLifecycleError("a device still serves this person")
        redacted = replace(
            person,
            display_name=REDACTED_DISPLAY_NAME,
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            status="disabled",
            updated_at=timestamp,
        )
        await self._store.redact_bound_subject(
            person_id=person_id,
            display_name=REDACTED_DISPLAY_NAME,
            updated_at=timestamp,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="person.redact",
                actor_person_id=actor_person_id,
                subject_person_id=person_id,
                person_id=person_id,
                device_id=None,
                binding_id=None,
                relationship_id=None,
                payload={"person_id": person_id, "status": "disabled"},
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
        )
        return redacted

    async def confirm_relationship(
        self,
        *,
        relationship_id: str,
        person_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        """Confirm one endpoint; the relationship activates after both sides."""
        timestamp = _now(now)
        return await self._with_concurrent_retry(
            self._confirm_once, relationship_id, person_id, timestamp
        )

    async def _confirm_once(
        self,
        relationship_id: str,
        person_id: str,
        timestamp: datetime,
    ) -> Relationship:
        relationship = await self._load_relationship(
            relationship_id, actor_person_id=person_id
        )
        if person_id not in (relationship.source_person_id, relationship.target_person_id):
            raise IdentityAccessDeniedError(
                f"person {person_id} is not an endpoint of {relationship_id}"
            )
        if relationship.status != "pending":
            raise RelationshipLifecycleError(
                f"only pending relationships can be confirmed (status={relationship.status})"
            )
        confirmed_source = relationship.confirmed_by_source_at
        confirmed_target = relationship.confirmed_by_target_at
        if person_id == relationship.source_person_id:
            if confirmed_source is not None:
                raise IdentityConflictError("source endpoint already confirmed")
            confirmed_source = timestamp
        else:
            if confirmed_target is not None:
                raise IdentityConflictError("target endpoint already confirmed")
            confirmed_target = timestamp
        activated = (
            confirmed_source is not None
            and confirmed_target is not None
        )
        updated = replace(
            relationship,
            status="active" if activated else relationship.status,
            confirmed_by_source_at=confirmed_source,
            confirmed_by_target_at=confirmed_target,
            updated_at=timestamp,
        )
        confirm_event_id = _new_id()
        await self._store.save_relationship(
            updated,
            audit_event=AuditEvent(
                event_id=confirm_event_id,
                action="relationship.confirm",
                actor_person_id=person_id,
                subject_person_id=(
                    relationship.target_person_id
                    if person_id == relationship.source_person_id
                    else relationship.source_person_id
                ),
                person_id=person_id,
                device_id=None,
                binding_id=None,
                relationship_id=relationship_id,
                payload=updated.to_dict(),
                created_at=timestamp,
            ),
            outbox_event=(
                OutboxEvent(
                    outbox_id=_new_id(),
                    event_id=confirm_event_id,
                    topic="identity.relationship.confirmed",
                    payload=updated.to_dict(),
                    created_at=timestamp,
                )
                if activated
                else None
            ),
            expected_updated_at=relationship.updated_at,
            actor_person_id=person_id,
        )
        return updated

    async def _with_concurrent_retry(
        self,
        fn: Callable[..., Awaitable[Relationship]],
        *args: object,
        **kwargs: object,
    ) -> Relationship:
        """Retry a CAS-guarded relationship transition on concurrent writes."""
        for attempt in range(3):
            try:
                return await fn(*args, **kwargs)
            except IdentityConflictError as exc:
                if "concurrently" not in str(exc) or attempt == 2:
                    raise
        raise IdentityConflictError("relationship transition kept conflicting")

    async def suspend_relationship(
        self,
        *,
        relationship_id: str,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        return await self._transition_relationship(
            relationship_id=relationship_id,
            actor_person_id=actor_person_id,
            target="suspended",
            now=now,
        )

    async def resume_relationship(
        self,
        *,
        relationship_id: str,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        return await self._transition_relationship(
            relationship_id=relationship_id,
            actor_person_id=actor_person_id,
            target="active",
            now=now,
        )

    async def dispute_relationship(
        self,
        *,
        relationship_id: str,
        actor_person_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> Relationship:
        timestamp = _now(now)
        relationship = await self._load_relationship(
            relationship_id, actor_person_id=actor_person_id
        )
        self._require_endpoint(relationship, actor_person_id)
        self._require_transition(relationship, "disputed")
        stripped_reason = reason.strip()
        if not stripped_reason:
            raise RelationshipLifecycleError(
                "dispute reason must not be empty"
            )
        updated = replace(
            relationship,
            status="disputed",
            dispute_reason=stripped_reason[:256],
            dispute_resolution_acked_by_source_at=None,
            dispute_resolution_acked_by_target_at=None,
            updated_at=timestamp,
        )
        return await self._save_transition(
            updated,
            actor_person_id,
            timestamp,
            expected_updated_at=relationship.updated_at,
        )

    async def acknowledge_dispute_resolution(
        self,
        *,
        relationship_id: str,
        person_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        """One endpoint acknowledges the dispute resolution.

        The relationship stays ``disputed`` until BOTH endpoints acknowledge;
        a single side can never restore an active relationship (acceptance 2).
        The acknowledgement trail lands in the audit payload because the ack
        timestamps are cleared once the relationship activates.
        """
        timestamp = _now(now)
        return await self._with_concurrent_retry(
            self._acknowledge_dispute_once,
            relationship_id,
            person_id,
            timestamp,
        )

    async def _acknowledge_dispute_once(
        self,
        relationship_id: str,
        person_id: str,
        timestamp: datetime,
    ) -> Relationship:
        relationship = await self._load_relationship(
            relationship_id, actor_person_id=person_id
        )
        self._require_endpoint(relationship, person_id)
        if relationship.status != "disputed":
            raise RelationshipLifecycleError(
                f"only disputed relationships can acknowledge resolution "
                f"(status={relationship.status})"
            )
        acked_source = relationship.dispute_resolution_acked_by_source_at
        acked_target = relationship.dispute_resolution_acked_by_target_at
        if person_id == relationship.source_person_id:
            if acked_source is not None:
                raise IdentityConflictError("source endpoint already acknowledged resolution")
            acked_source = timestamp
        else:
            if acked_target is not None:
                raise IdentityConflictError("target endpoint already acknowledged resolution")
            acked_target = timestamp
        resolved = acked_source is not None and acked_target is not None
        payload_extra: dict[str, object] = {
            "resolution_acked_by_source_at": (
                acked_source.isoformat() if acked_source is not None else None
            ),
            "resolution_acked_by_target_at": (
                acked_target.isoformat() if acked_target is not None else None
            ),
            "resolution_complete": resolved,
        }
        updated = replace(
            relationship,
            status="active" if resolved else "disputed",
            dispute_reason=None if resolved else relationship.dispute_reason,
            dispute_resolution_acked_by_source_at=(
                None if resolved else acked_source
            ),
            dispute_resolution_acked_by_target_at=(
                None if resolved else acked_target
            ),
            updated_at=timestamp,
        )
        return await self._save_transition(
            updated,
            person_id,
            timestamp,
            payload_extra=payload_extra,
            expected_updated_at=relationship.updated_at,
        )

    async def revoke_relationship(
        self,
        *,
        relationship_id: str,
        actor_person_id: str,
        evidence_id: str,
        now: datetime | None = None,
    ) -> Relationship:
        timestamp = _now(now)
        relationship = await self._load_relationship(
            relationship_id, actor_person_id=actor_person_id
        )
        self._require_endpoint(relationship, actor_person_id)
        self._require_transition(relationship, "revoked")
        updated = replace(
            relationship,
            status="revoked",
            revoked_at=timestamp,
            revocation_evidence_id=evidence_id,
            updated_at=timestamp,
        )
        return await self._save_transition(
            updated,
            actor_person_id,
            timestamp,
            expected_updated_at=relationship.updated_at,
        )

    async def expire_relationships(self, now: datetime | None = None) -> int:
        timestamp = _now(now)
        candidates = await self._store.scan_relationships(
            statuses=("pending", "active", "suspended", "disputed"),
            scope="migration",
        )
        count = 0
        for relationship in candidates:
            if relationship.valid_until is not None and relationship.valid_until <= timestamp:
                await self._save_transition(
                    replace(relationship, status="expired", updated_at=timestamp),
                    relationship.source_person_id,
                    timestamp,
                    scope="migration",
                    expected_updated_at=relationship.updated_at,
                )
                count += 1
        return count

    async def list_relationships(
        self,
        *,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        actor_person_id: str | None = None,
    ) -> tuple[Relationship, ...]:
        return await self._store.list_relationships(
            person_id,
            statuses=statuses,
            actor_person_id=actor_person_id or person_id,
        )

    async def declared_guardians(
        self,
        *,
        subject_person_id: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Guardians with a binding-issued declaration or attestation for the subject.

        Scoped to the owner of an ACTIVE binding naming the subject, so a third
        party that learns the person id never qualifies. It reaches the person
        who took responsibility; it is never verified guardianship or consent.
        """

        return await self._binding_scoped_sources(
            subject_person_id, now, lambda r: (r.relation_type, r.status, r.established_evidence_id)
            in _CRISIS_CONTACT_LINKS["guardian_of"],
        )

    async def crisis_contacts(
        self,
        *,
        subject_person_id: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Declared guardians plus an elder's binding delegate (P0-04 D7), same scope."""

        return await self._binding_scoped_sources(
            subject_person_id, now, lambda r: (r.relation_type, r.status, r.established_evidence_id)
            in _CRISIS_CONTACT_LINKS["guardian_of"] | _CRISIS_CONTACT_LINKS["delegate_for"],
        )

    async def _binding_scoped_sources(
        self,
        subject_person_id: str,
        now: datetime | None,
        accepted: Callable[[Relationship], bool],
    ) -> tuple[str, ...]:
        """Sources of accepted one-sided links who own an ACTIVE binding for the subject."""

        timestamp = _now(now)
        relationships = await self._store.list_relationships(
            subject_person_id,
            statuses=("pending", "active"),
            actor_person_id=subject_person_id,
        )
        candidates = {
            r.source_person_id
            for r in relationships
            if r.target_person_id == subject_person_id
            and r.confirmed_by_source_at is not None
            and r.confirmed_by_target_at is None
            and r.valid_from <= timestamp
            and (r.valid_until is None or timestamp < r.valid_until)
            and accepted(r)
        }
        if not candidates:
            return ()
        bindings = await self._store.list_active_bindings_for_person(
            subject_person_id, timestamp, actor_person_id=subject_person_id
        )
        owners = {
            binding.account_owner_person_id
            for binding in bindings
            if subject_person_id in binding.primary_subject_ids
        }
        return tuple(sorted(candidates & owners))

    async def _load_relationship(
        self,
        relationship_id: str,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> Relationship:
        relationship = await self._store.get_relationship(
            relationship_id,
            actor_person_id=actor_person_id,
            scope=scope,
        )
        if relationship is None:
            raise IdentityNotFoundError(
                f"relationship {relationship_id} does not exist"
            )
        return relationship

    @staticmethod
    def _require_endpoint(relationship: Relationship, person_id: str) -> None:
        if person_id not in (relationship.source_person_id, relationship.target_person_id):
            raise IdentityAccessDeniedError(
                f"person {person_id} is not an endpoint of {relationship.relationship_id}"
            )

    @staticmethod
    def _require_transition(
        relationship: Relationship, target: RelationshipStatus
    ) -> None:
        allowed = {
            "active": ("suspended", "revoked", "expired", "disputed"),
            "suspended": ("active", "revoked", "expired", "disputed"),
            "disputed": ("active", "revoked", "expired"),
            "pending": ("revoked", "expired", "disputed"),
            "revoked": (),
            "expired": (),
        }[relationship.status]
        if target not in allowed:
            raise RelationshipLifecycleError(
                f"cannot transition relationship from {relationship.status} "
                f"to {target}"
            )

    async def _transition_relationship(
        self,
        *,
        relationship_id: str,
        actor_person_id: str,
        target: RelationshipStatus,
        now: datetime | None,
    ) -> Relationship:
        timestamp = _now(now)
        relationship = await self._load_relationship(
            relationship_id, actor_person_id=actor_person_id
        )
        self._require_endpoint(relationship, actor_person_id)
        self._require_transition(relationship, target)
        explicit_resume = (
            target == "active"
            and relationship.status == "suspended"
            and relationship.auto_suspended
        )
        return await self._save_transition(
            replace(
                relationship,
                status=target,
                updated_at=timestamp,
                dispute_resolution_acked_by_source_at=None,
                dispute_resolution_acked_by_target_at=None,
                auto_suspended=(
                    False
                    if explicit_resume or target == "suspended"
                    else relationship.auto_suspended
                ),
            ),
            actor_person_id,
            timestamp,
            expected_updated_at=relationship.updated_at,
        )

    async def _save_transition(
        self,
        relationship: Relationship,
        actor_person_id: str,
        timestamp: datetime,
        *,
        payload_extra: dict[str, object] | None = None,
        scope: str = "api",
        expected_updated_at: datetime | None = None,
    ) -> Relationship:
        payload = relationship.to_dict()
        if payload_extra:
            payload.update(payload_extra)
        extra_relationships, extra_audit_events = await self._build_cascade(
            relationship, timestamp, actor_person_id, scope=scope
        )
        await self._store.save_relationship(
            relationship,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action=f"relationship.{relationship.status}",
                actor_person_id=actor_person_id,
                subject_person_id=relationship.target_person_id,
                person_id=relationship.source_person_id,
                device_id=None,
                binding_id=None,
                relationship_id=relationship.relationship_id,
                payload=payload,
                created_at=timestamp,
            ),
            extra_relationships=extra_relationships,
            extra_audit_events=extra_audit_events,
            expected_updated_at=expected_updated_at,
            actor_person_id=actor_person_id,
            scope=scope,
        )
        return relationship

    async def _build_cascade(
        self,
        parent: Relationship,
        timestamp: datetime,
        actor_person_id: str,
        *,
        scope: str,
    ) -> tuple[tuple[Relationship, ...], tuple[AuditEvent, ...]]:
        """Fail-closed delegation cascade in the parent's transaction.

        - parent active: restore children that were auto-suspended by cascade;
        - parent revoked/expired: children become revoked (terminal);
        - parent suspended/disputed: children become auto-suspended.
        """
        if parent.status not in (
            "active",
            "suspended",
            "disputed",
            "revoked",
            "expired",
        ):
            return (), ()
        # Recursive fail-closed cascade: every descendant (depth <= 3) is
        # collected before the parent transition is written, so the whole
        # subtree lands in one transaction.
        frontier = [parent.relationship_id]
        children: list[Relationship] = []
        seen: set[str] = set()
        for _ in range(MAX_DELEGATION_DEPTH + 1):
            if not frontier:
                break
            next_frontier: list[str] = []
            for relationship_id in frontier:
                for item in await self._store.scan_relationships(
                    statuses=("active", "suspended"),
                    actor_person_id=actor_person_id,
                    scope=scope,
                ):
                    if (
                        item.delegated_from_relationship_id == relationship_id
                        and item.relationship_id not in seen
                    ):
                        seen.add(item.relationship_id)
                        children.append(item)
                        next_frontier.append(item.relationship_id)
            frontier = next_frontier
        if not children:
            return (), ()
        updates: list[Relationship] = []
        audits: list[AuditEvent] = []
        for child in children:
            if parent.status == "active":
                if child.status == "active" or not child.auto_suspended:
                    continue
                updated = replace(
                    child,
                    status="active",
                    auto_suspended=False,
                    updated_at=timestamp,
                )
            elif parent.status in ("revoked", "expired"):
                if child.status not in ("active", "suspended"):
                    continue
                updated = replace(
                    child,
                    status="revoked",
                    auto_suspended=False,
                    revoked_at=timestamp,
                    revocation_evidence_id=(
                        f"parent:{parent.relationship_id}:{parent.status}"
                    ),
                    updated_at=timestamp,
                )
            else:  # suspended / disputed
                if child.status not in ("active", "suspended"):
                    continue
                updated = replace(
                    child,
                    status="suspended",
                    auto_suspended=True,
                    dispute_resolution_acked_by_source_at=None,
                    dispute_resolution_acked_by_target_at=None,
                    updated_at=timestamp,
                )
            updates.append(updated)
            audits.append(
                AuditEvent(
                    event_id=_new_id(),
                    action=f"relationship.{updated.status}",
                    actor_person_id=actor_person_id,
                    subject_person_id=updated.target_person_id,
                    person_id=updated.source_person_id,
                    device_id=None,
                    binding_id=None,
                    relationship_id=updated.relationship_id,
                    payload=updated.to_dict(),
                    created_at=timestamp,
                )
            )
        return tuple(updates), tuple(audits)
