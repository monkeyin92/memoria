"""Application facade for the identity domain.

``IdentityService`` is the only entry point Control API needs: it executes
create / supersede / transfer / revoke / get-active and returns serializable
``BindingManifest`` objects, appending audit and transactional-outbox events
for every mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.common.companions import COMPANION_IDS
from services.consent.binding_snapshot import (
    BindingConsentCommand,
    BindingConsentRole,
    BindingRelationshipEvidence,
)
from services.identity.authority import (
    TRANSFER_DEFAULT_TTL,
    TRANSFER_MAX_TTL,
    ConsentAuthorityUnavailableError,
    ConsentSnapshotResolver,
    RejectingConsentSnapshotResolver,
    RejectingTransferEvidenceVerifier,
    TransferEvidenceVerifier,
    TransferVerificationError,
)
from services.identity.domain import (
    ALL_BINDING_ROLES,
    ALL_PERMISSIONS,
    MAX_DELEGATION_DEPTH,
    ROLE_DEFAULT_PERMISSIONS,
    AgeBand,
    AgeEvidenceError,
    AgeEvidenceStatus,
    BindingManifest,
    BindingReason,
    BindingRole,
    DeviceBinding,
    DeviceBindingRole,
    DeviceDeclaredMode,
    IdempotencyRecord,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    ModeConstraintError,
    Permission,
    PersonaAssignmentRecord,
    PersonSubject,
    Relationship,
    RelationshipLifecycleError,
    RelationshipStatus,
    RelationType,
    RoleConstraintError,
    SubjectCategory,
    TransferIntent,
    TransferLifecycleError,
    canonical_persona_assignment_id,
    derive_subject_category,
    has_permission,
    manifest_from_binding,
    manifest_from_dict,
    transfer_from_dict,
    validate_age_declaration,
    validate_manifest_wire,
)
from services.identity.repository import AuditEvent, IdentityStore, OutboxEvent


def _now(value: datetime | None) -> datetime:
    return value.astimezone(UTC) if value is not None else datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


# The built-in companion catalogue is the only persona source today.  The
# frozen per-account custom personas of increment two widen this set; the
# decision record lives in
# ``outputs/design/persona-switch-20260911/decision-01-persona-catalogue-freeze.md``.
_KNOWN_PERSONA_IDS: frozenset[str] = COMPANION_IDS
_PERSONA_VERSION_PREFIX = ":v"


def _resolve_persona_selection(persona_selection: str) -> tuple[str, int]:
    """Split ``"{persona_id}"`` / ``"{persona_id}:v{n}"`` into its two parts.

    A bare persona id is the first version (``v1``); the canonical assignment
    id is validated here so no caller can persist a second spelling.
    """
    persona_id, separator, raw_version = persona_selection.strip().partition(
        _PERSONA_VERSION_PREFIX
    )
    if persona_id not in _KNOWN_PERSONA_IDS:
        raise ValueError(f"unknown persona_selection {persona_id!r}")
    if separator:
        if not raw_version.isdigit():
            raise ValueError("persona_selection version must be a positive integer")
        persona_version = int(raw_version)
    else:
        persona_version = 1
    canonical_persona_assignment_id(persona_id, persona_version)
    return persona_id, persona_version


def _request_hash(**fields: object) -> str:
    """Canonical digest of an idempotent command request."""
    return hashlib.sha256(
        json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _replay_or_conflict(
    existing: IdempotencyRecord,
    *,
    content_hash: str,
) -> None:
    if existing.content_hash != content_hash:
        raise IdentityConflictError(
            "idempotency key reused with different content"
        )


def _same_person_content(left: PersonSubject, right: PersonSubject) -> bool:
    """Authoritative registration fields; timestamps are not content."""
    return (
        left.person_id == right.person_id
        and left.display_name == right.display_name
        and left.subject_category == right.subject_category
        and left.age_band == right.age_band
        and left.age_evidence_status == right.age_evidence_status
        and left.locale == right.locale
        and left.timezone == right.timezone
        and left.status == right.status
    )


def _canonical_registration_result(
    stored: PersonSubject, requested: PersonSubject
) -> PersonSubject:
    """Idempotent registration replay: return the persisted row only.

    An exact replay returns the actually persisted object; any drift between
    the request and the persisted authoritative fields is an explicit
    conflict -- the caller never receives an unpersisted request object.
    """
    if not _same_person_content(stored, requested):
        raise IdentityConflictError(
            f"person {requested.person_id} already registered with different "
            "authoritative fields (display_name/age/category/locale/timezone)"
        )
    return stored


class IdentityService:
    """Deep-module facade: persons, relationships and versioned bindings."""

    def __init__(
        self,
        store: IdentityStore,
        *,
        transfer_verifier: TransferEvidenceVerifier | None = None,
        consent_resolver: ConsentSnapshotResolver | None = None,
    ) -> None:
        self._store = store
        self._transfer_verifier = (
            transfer_verifier or RejectingTransferEvidenceVerifier()
        )
        self._consent_resolver = (
            consent_resolver or RejectingConsentSnapshotResolver()
        )

    # ------------------------------------------------------------------
    # Persons
    # ------------------------------------------------------------------

    async def register_person(
        self,
        *,
        display_name: str,
        timezone: str,
        locale: str = "zh-CN",
        subject_category: SubjectCategory | None = None,
        age_band: AgeBand = "unknown",
        age_evidence_status: AgeEvidenceStatus = "unverified",
        age_evidence_id: str | None = None,
        person_id: str | None = None,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> PersonSubject:
        """Register a person; unknown defaults fail closed (never adult)."""
        timestamp = _now(now)
        resolved_category = (
            derive_subject_category(
                age_band=age_band, age_evidence_status=age_evidence_status
            )
            if subject_category is None
            else subject_category
        )
        validate_age_declaration(
            subject_category=resolved_category,
            age_band=age_band,
            age_evidence_status=age_evidence_status,
        )
        if (
            resolved_category == "adult"
            and age_evidence_status == "verified"
            and (not age_evidence_id or len(age_evidence_id) > 128)
        ):
            raise AgeEvidenceError(
                "verified adult registration requires an authoritative "
                "age evidence id"
            )
        person = PersonSubject(
            person_id=person_id or _new_id(),
            display_name=display_name,
            subject_category=resolved_category,
            age_band=age_band,
            age_evidence_status=age_evidence_status,
            locale=locale,
            timezone=timezone,
            created_at=timestamp,
            updated_at=timestamp,
        )
        scope = (
            "api"
            if actor_person_id is not None and actor_person_id == person.person_id
            else "registration"
            if actor_person_id is not None
            else "internal"
        )
        stored = await self._store.register_person(
            person,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="person.register",
                actor_person_id=actor_person_id,
                subject_person_id=person.person_id,
                person_id=person.person_id,
                device_id=None,
                binding_id=None,
                relationship_id=None,
                payload=person.to_dict(),
                created_at=timestamp,
            ),
            outbox_event=OutboxEvent(
                outbox_id=_new_id(),
                event_id=_new_id(),
                topic="identity.person.registered",
                payload=person.to_dict(),
                created_at=timestamp,
            ),
            evidence_id=age_evidence_id,
            actor_person_id=actor_person_id,
            scope=scope,
        )
        return _canonical_registration_result(stored, person)

    async def get_person(
        self, person_id: str, actor_person_id: str | None = None
    ) -> PersonSubject:
        # External reads require an explicit actor.  Without one the read
        # runs in the internal scope: the SQLite adapters still resolve the
        # row (dev parity), the PostgreSQL adapter hides it (fail closed) so
        # nobody can impersonate a target through a missing actor.
        person = await self._store.get_person(
            person_id,
            actor_person_id=actor_person_id,
            scope="internal" if actor_person_id is None else "api",
        )
        if person is None:
            raise IdentityNotFoundError(f"person {person_id} does not exist")
        return person

    async def declare_age_evidence(
        self,
        *,
        person_id: str,
        age_band: AgeBand,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> PersonSubject:
        """Ordinary age DECLARATION by the person or an active guardian.

        Declarations can never claim adult or verified evidence: the outcome
        is at most unknown/minor + unverified (a contradiction against a
        verified adult downgrades into disputed).  Authoritative adult
        verification is a separate action with evidence + a verified-adult
        verifier (``verify_age_evidence``).
        """
        timestamp = _now(now)
        if age_band not in ("unknown", "under_14", "14_17"):
            raise AgeEvidenceError(
                "age declarations cannot claim adult; use authoritative "
                "age verification with evidence"
            )
        if actor_person_id is None:
            raise AgeEvidenceError(
                "age declarations require an authenticated actor"
            )
        person = await self.get_person(
            person_id, actor_person_id=actor_person_id
        )
        if (
            person.subject_category == "adult"
            and person.age_evidence_status == "verified"
        ):
            # Contradictory declaration against a verified adult: fail
            # closed into disputed/unknown.
            declared_band: AgeBand = "unknown"
            declared_evidence: AgeEvidenceStatus = "disputed"
        else:
            declared_band = age_band
            declared_evidence = "unverified"
        category = derive_subject_category(
            age_band=declared_band, age_evidence_status=declared_evidence
        )
        validate_age_declaration(
            subject_category=category,
            age_band=declared_band,
            age_evidence_status=declared_evidence,
        )
        updated = replace(
            person,
            subject_category=category,
            age_band=declared_band,
            age_evidence_status=declared_evidence,
            updated_at=timestamp,
        )
        await self._store.declare_age_evidence(
            updated,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="person.age_evidence.update",
                actor_person_id=actor_person_id,
                subject_person_id=person_id,
                person_id=person_id,
                device_id=None,
                binding_id=None,
                relationship_id=None,
                payload=updated.to_dict(),
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )
        return updated

    async def verify_age_evidence(
        self,
        *,
        person_id: str,
        evidence_id: str,
        verifier_person_id: str,
        now: datetime | None = None,
    ) -> PersonSubject:
        """Authoritative age verification: only a verified adult verifier
        with a non-empty evidence id may verify someone as an adult.

        Self-verification is impossible (distinct verifier), ordinary
        declarations can never reach verified adult, and the PostgreSQL
        verification port is executable only by the dedicated registration
        authority.  The evidence-carrying audit/outbox trail is written in
        the same transaction.
        """
        timestamp = _now(now)
        if not evidence_id or len(evidence_id) > 128:
            raise AgeEvidenceError(
                "age verification requires a non-empty evidence id "
                "(<=128 chars)"
            )
        if not verifier_person_id or verifier_person_id == person_id:
            raise AgeEvidenceError(
                "age verification requires a distinct verifier person"
            )
        verifier = await self.get_person(
            verifier_person_id, actor_person_id=verifier_person_id
        )
        if (
            verifier.subject_category != "adult"
            or verifier.age_evidence_status != "verified"
        ):
            raise AgeEvidenceError(
                "age verification requires a verified adult verifier"
            )
        person = await self.get_person(
            person_id, actor_person_id=verifier_person_id
        )
        updated = replace(
            person,
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            updated_at=timestamp,
        )
        validate_age_declaration(
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
        )
        await self._store.verify_age_evidence(
            updated,
            evidence_id=evidence_id,
            verifier_person_id=verifier_person_id,
            actor_person_id=verifier_person_id,
            scope="registration",
        )
        return updated

    async def reconcile_account_registration(
        self,
        *,
        person_id: str,
        evidence_id: str,
        source_revision: int,
        now: datetime | None = None,
    ) -> PersonSubject:
        """Mirror the Control plane's existing WeChat registration decision.

        This server-only registration action is NOT human age verification or
        speaker authentication. Its receipt must come from persisted Control
        phone binding + subject revision, never request-body age fields. The
        ordinary distinct-verifier age-verification port remains unchanged.
        """
        if (
            not re.fullmatch(r"control-wechat-phone-v1:[a-f0-9]{64}", evidence_id)
            or type(source_revision) is not int
            or source_revision < 1
        ):
            raise AgeEvidenceError("persisted Control registration evidence required")
        person = await self.get_person(person_id, actor_person_id=person_id)
        if person.status != "active":
            raise IdentityConflictError("inactive registration cannot be reconciled")
        current = (person.subject_category, person.age_band, person.age_evidence_status)
        if current == ("adult", "adult", "verified"):
            return person
        if current != ("unknown", "unknown", "unverified"):
            raise IdentityConflictError("registration conflicts with existing age evidence")
        timestamp = max(_now(now), person.updated_at + timedelta(microseconds=1))
        updated = replace(
            person, subject_category="adult", age_band="adult",
            age_evidence_status="verified", updated_at=timestamp,
        )
        event_id = _new_id()
        payload: dict[str, object] = {
            **updated.to_dict(),
            "source": "control.wechat_phone_registration.v1",
            "evidence_id": evidence_id,
            "source_profile_revision": source_revision,
            "previous_subject_category": person.subject_category,
            "previous_age_band": person.age_band,
            "previous_age_evidence_status": person.age_evidence_status,
        }
        await self._store.reconcile_account_registration(
            updated, expected_updated_at=person.updated_at,
            evidence_id=evidence_id, source_revision=source_revision,
            audit_event=AuditEvent(
                event_id=event_id, action="person.registration_reconciled",
                actor_person_id=person_id, subject_person_id=person_id,
                person_id=person_id, device_id=None, binding_id=None,
                relationship_id=None, payload=payload, created_at=timestamp,
            ),
            outbox_event=OutboxEvent(
                outbox_id=_new_id(), event_id=event_id,
                topic="identity.person.registration_reconciled",
                payload=payload, created_at=timestamp,
            ),
        )
        return await self.get_person(person_id, actor_person_id=person_id)

    async def update_person_profile(
        self,
        *,
        person_id: str,
        display_name: str | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> PersonSubject:
        """Self-service profile update: display_name/locale/timezone only."""
        timestamp = _now(now)
        if actor_person_id is None or actor_person_id != person_id:
            raise IdentityAccessDeniedError(
                "profile updates are self-service only"
            )
        person = await self.get_person(
            person_id, actor_person_id=actor_person_id
        )
        updated = replace(
            person,
            display_name=display_name if display_name is not None else person.display_name,
            locale=locale if locale is not None else person.locale,
            timezone=timezone if timezone is not None else person.timezone,
            updated_at=timestamp,
        )
        await self._store.update_person_profile(
            updated,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="person.profile.update",
                actor_person_id=actor_person_id,
                subject_person_id=person_id,
                person_id=person_id,
                device_id=None,
                binding_id=None,
                relationship_id=None,
                payload=updated.to_dict(),
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )
        return updated

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    async def create_binding(
        self,
        *,
        device_id: str,
        declared_mode: DeviceDeclaredMode,
        account_owner_person_id: str,
        primary_subject_ids: tuple[str, ...],
        service_profile_version: str,
        policy_bundle_version: str,
        roles: tuple[tuple[str, BindingRole], ...] | None = None,
        role_permissions: dict[tuple[str, BindingRole], frozenset[Permission]]
        | None = None,
        family_space_id: str | None = None,
        consent_offer_ids: tuple[str, ...] = (),
        service_preferences: dict[str, object] | None = None,
        persona_assignment_id: str | None = None,
        valid_until: datetime | None = None,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> BindingManifest:
        """Create the first binding version of a device."""
        timestamp = _now(now)
        binding = await self._build_binding(
            device_id=device_id,
            declared_mode=declared_mode,
            account_owner_person_id=account_owner_person_id,
            primary_subject_ids=primary_subject_ids,
            roles=roles or (),
            role_permissions=role_permissions,
            family_space_id=family_space_id,
            service_profile_version=service_profile_version,
            policy_bundle_version=policy_bundle_version,
            consent_snapshot_id=None,
            persona_assignment_id=persona_assignment_id,
            reason="create",
            supersedes_binding_id=None,
            valid_from=timestamp,
            valid_until=valid_until,
            now=timestamp,
        )
        relationship_evidence = await self._binding_relationship_evidence(
            binding=binding,
            actor_person_id=actor_person_id or account_owner_person_id,
            now=timestamp,
        )
        # Client-supplied snapshot ids are not accepted.  The server authority
        # receives the already-validated candidate so the snapshot is bound to
        # the real binding id, roles and evidence revision.
        consent_snapshot_id = await self._consent_resolver.resolve(
            command=BindingConsentCommand(
                actor_person_id=actor_person_id or account_owner_person_id,
                account_owner_person_id=binding.account_owner_person_id,
                primary_subject_ids=binding.primary_subject_ids,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                binding_version=binding.binding_version,
                declared_mode=binding.declared_mode,
                roles=tuple(
                    BindingConsentRole(
                        person_id=role.person_id,
                        role=role.role,
                        permissions=tuple(role.permissions),
                    )
                    for role in binding.roles
                ),
                consent_offer_ids=consent_offer_ids,
                service_preferences=service_preferences or {},
                relationship_evidence=relationship_evidence,
                service_profile_version=binding.service_profile_version,
                policy_bundle_version=binding.policy_bundle_version,
                issued_at=timestamp,
            )
        )
        if consent_snapshot_id is None:
            raise ConsentAuthorityUnavailableError(
                "consent authority unavailable; binding fails closed"
            )
        binding = replace(binding, consent_snapshot_id=consent_snapshot_id)
        audit, outbox = self._binding_events(
            action="binding.create",
            manifest=manifest_from_binding(binding),
            actor_person_id=account_owner_person_id,
            timestamp=timestamp,
        )
        persisted = await self._store.persist_binding(
            binding,
            audit_event=audit,
            outbox_event=outbox,
            actor_person_id=actor_person_id or account_owner_person_id,
        )
        manifest = manifest_from_binding(persisted)
        validate_manifest_wire(manifest)
        return manifest

    async def supersede_binding(
        self,
        *,
        device_id: str,
        declared_mode: DeviceDeclaredMode,
        account_owner_person_id: str | None = None,
        primary_subject_ids: tuple[str, ...],
        service_profile_version: str | None = None,
        policy_bundle_version: str | None = None,
        roles: tuple[tuple[str, BindingRole], ...] | None = None,
        role_permissions: dict[tuple[str, BindingRole], frozenset[Permission]]
        | None = None,
        family_space_id: str | None = None,
        consent_snapshot_id: str | None = None,
        persona_assignment_id: str | None = None,
        valid_until: datetime | None = None,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> BindingManifest:
        """Issue the next binding version (mode / subject / member change)."""
        timestamp = _now(now)
        current = await self._require_active_binding(
            device_id, timestamp, actor_person_id=actor_person_id
        )
        await self._require_permission(current, actor_person_id, "binding.manage")
        resolved_owner = account_owner_person_id or current.account_owner_person_id
        resolved_profile = (
            service_profile_version or current.service_profile_version
        )
        resolved_policy = policy_bundle_version or current.policy_bundle_version
        resolved_persona = persona_assignment_id or current.persona_assignment_id
        # Server-side authority fields: consent snapshot always carries over
        # from the current binding; the client cannot inject a new one.
        binding = await self._build_binding(
            device_id=device_id,
            declared_mode=declared_mode,
            account_owner_person_id=resolved_owner,
            primary_subject_ids=primary_subject_ids,
            roles=roles or (),
            role_permissions=role_permissions,
            family_space_id=family_space_id,
            service_profile_version=resolved_profile,
            policy_bundle_version=resolved_policy,
            consent_snapshot_id=current.consent_snapshot_id,
            persona_assignment_id=resolved_persona,
            reason="supersede",
            supersedes_binding_id=current.binding_id,
            valid_from=timestamp,
            valid_until=valid_until,
            now=timestamp,
        )
        audit, outbox = self._binding_events(
            action="binding.supersede",
            manifest=manifest_from_binding(binding),
            actor_person_id=actor_person_id,
            timestamp=timestamp,
        )
        persisted = await self._store.persist_binding(
            binding,
            previous_binding_id=current.binding_id,
            audit_event=audit,
            outbox_event=outbox,
            actor_person_id=actor_person_id,
        )
        manifest = manifest_from_binding(persisted)
        validate_manifest_wire(manifest)
        return manifest

    async def revoke_binding(
        self,
        *,
        device_id: str,
        actor_person_id: str,
        reason: str = "unbind",
        now: datetime | None = None,
    ) -> DeviceBinding:
        """Unbind a device; the revoked version stays in the audit trail."""
        timestamp = _now(now)
        current = await self._require_active_binding(
            device_id, timestamp, actor_person_id=actor_person_id
        )
        await self._require_permission(current, actor_person_id, "binding.manage")
        audit, outbox = self._binding_events(
            action="binding.revoke",
            manifest=manifest_from_binding(current),
            actor_person_id=actor_person_id,
            timestamp=timestamp,
            extra={"revoke_reason": reason},
        )
        revoked = await self._store.transition_binding(
            current.binding_id,
            status="revoked",
            valid_until=timestamp if timestamp > current.valid_from else None,
            at=timestamp,
            audit_event=audit,
            outbox_event=outbox,
            actor_person_id=actor_person_id,
        )
        return revoked

    async def expire_bindings(self, now: datetime | None = None) -> int:
        timestamp = _now(now)
        count = 0
        for binding in await self._store.scan_bindings(
            statuses=("active",), scope="migration"
        ):
            if binding.valid_until is not None and binding.valid_until <= timestamp:
                audit, outbox = self._binding_events(
                    action="binding.expire",
                    manifest=manifest_from_binding(binding),
                    actor_person_id=None,
                    timestamp=timestamp,
                )
                await self._store.transition_binding(
                    binding.binding_id,
                    status="expired",
                    valid_until=binding.valid_until,
                    at=timestamp,
                    audit_event=audit,
                    outbox_event=outbox,
                    actor_person_id=None,
                    scope="migration",
                )
                count += 1
        return count

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

    async def get_active_binding(
        self, device_id: str, now: datetime | None = None
    ) -> DeviceBinding | None:
        return await self._store.get_active_binding(device_id, _now(now))

    async def get_active_manifest(
        self,
        device_id: str,
        now: datetime | None = None,
        actor_person_id: str | None = None,
    ) -> BindingManifest | None:
        binding = await self._store.get_active_binding(
            device_id, _now(now), actor_person_id=actor_person_id
        )
        return manifest_from_binding(binding) if binding is not None else None

    async def list_active_manifests_for_person(
        self,
        person_id: str,
        now: datetime | None = None,
        actor_person_id: str | None = None,
    ) -> tuple[BindingManifest, ...]:
        bindings = await self._store.list_active_bindings_for_person(
            person_id,
            _now(now),
            actor_person_id=actor_person_id or person_id,
        )
        manifests = tuple(manifest_from_binding(binding) for binding in bindings)
        for manifest in manifests:
            validate_manifest_wire(manifest)
        return manifests

    async def get_binding(
        self, binding_id: str, actor_person_id: str | None = None
    ) -> DeviceBinding:
        binding = await self._store.get_binding(
            binding_id, actor_person_id=actor_person_id
        )
        if binding is None:
            raise IdentityNotFoundError(f"binding {binding_id} does not exist")
        return binding

    async def list_binding_versions(
        self,
        device_id: str,
        actor_person_id: str | None = None,
    ) -> tuple[BindingManifest, ...]:
        bindings = await self._store.list_binding_versions(
            device_id, actor_person_id=actor_person_id
        )
        return tuple(manifest_from_binding(binding) for binding in bindings)

    # ------------------------------------------------------------------
    # Persona assignments (binding + subject scoped)
    # ------------------------------------------------------------------

    async def set_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        persona_selection: str,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> PersonaAssignmentRecord:
        """Pin one persona to one subject on one binding.

        ``(binding_id, subject_id)`` is the key: a subject holds at most one
        persona on a binding.  Replaying the same persona is idempotent and
        returns the persisted row untouched; a different persona replaces the
        override while keeping the original ``created_at``.

        Authorization stays in Control API (``_can_switch_subject`` plus
        ``_is_binding_member``); this facade only enforces that the binding
        exists and the persona is a known one.
        """
        timestamp = _now(now)
        binding = await self.get_binding(
            binding_id, actor_person_id=actor_person_id
        )
        persona_id, persona_version = _resolve_persona_selection(persona_selection)
        existing = await self._store.get_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            actor_person_id=actor_person_id,
        )
        record = PersonaAssignmentRecord(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            assignment_id=canonical_persona_assignment_id(
                persona_id, persona_version
            ),
            persona_id=persona_id,
            persona_version=persona_version,
            created_at=existing.created_at if existing is not None else timestamp,
            updated_at=timestamp,
        )
        payload: dict[str, object] = record.to_dict()
        if existing is not None:
            payload["previous_persona_id"] = existing.persona_id
        return await self._store.upsert_persona_assignment(
            record,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="binding.persona_assignment.set",
                actor_person_id=actor_person_id,
                subject_person_id=subject_id,
                person_id=subject_id,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                relationship_id=None,
                payload=payload,
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )

    async def get_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        actor_person_id: str | None = None,
    ) -> PersonaAssignmentRecord | None:
        """Return the subject override, or ``None`` for the binding default."""
        return await self._store.get_persona_assignment(
            binding_id=binding_id,
            subject_id=subject_id,
            actor_person_id=actor_person_id,
        )

    async def list_persona_assignments(
        self,
        *,
        binding_id: str,
        actor_person_id: str | None = None,
    ) -> tuple[PersonaAssignmentRecord, ...]:
        """Every subject override on one binding, ordered by subject id."""
        return await self._store.list_persona_assignments(
            binding_id=binding_id, actor_person_id=actor_person_id
        )

    async def delete_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Drop a subject override; ``False`` when there was none to drop."""
        timestamp = _now(now)
        binding = await self.get_binding(
            binding_id, actor_person_id=actor_person_id
        )
        existing = await self._store.get_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            actor_person_id=actor_person_id,
        )
        if existing is None:
            return False
        return await self._store.delete_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="binding.persona_assignment.delete",
                actor_person_id=actor_person_id,
                subject_person_id=subject_id,
                person_id=subject_id,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                relationship_id=None,
                payload=existing.to_dict(),
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _binding_relationship_evidence(
        self,
        *,
        binding: DeviceBinding,
        actor_person_id: str,
        now: datetime,
    ) -> tuple[BindingRelationshipEvidence, ...]:
        """Freeze active relationship rows used by the binding decision."""
        participants = {
            binding.account_owner_person_id,
            *binding.primary_subject_ids,
            *(role.person_id for role in binding.roles),
        }
        relationships: dict[str, Relationship] = {}
        for person_id in sorted(participants):
            visible = await self._store.list_relationships(
                person_id,
                statuses=("active",),
                actor_person_id=actor_person_id,
                scope="api",
            )
            for relationship in visible:
                if (
                    relationship.source_person_id in participants
                    and relationship.target_person_id in participants
                    and relationship.valid_from <= now
                    and (
                        relationship.valid_until is None
                        or now < relationship.valid_until
                    )
                ):
                    relationships[relationship.relationship_id] = relationship
        return tuple(
            BindingRelationshipEvidence(
                relationship_id=item.relationship_id,
                source_person_id=item.source_person_id,
                target_person_id=item.target_person_id,
                relation_type=item.relation_type,
                status=item.status,
                established_evidence_id=item.established_evidence_id,
                revision_at=item.updated_at,
                valid_from=item.valid_from,
                valid_until=item.valid_until,
            )
            for item in sorted(
                relationships.values(),
                key=lambda relationship: relationship.relationship_id,
            )
        )

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
    ) -> DeviceBinding:
        if not primary_subject_ids:
            raise ModeConstraintError("a binding requires at least one primary subject")
        if not await self._store.person_exists(account_owner_person_id):
            raise IdentityNotFoundError(
                f"person {account_owner_person_id} does not exist"
            )
        primary_ids = tuple(dict.fromkeys(primary_subject_ids))
        for person_id in primary_ids:
            if not await self._store.person_exists(person_id):
                raise IdentityNotFoundError(f"person {person_id} does not exist")
        grants: dict[tuple[str, BindingRole], frozenset[Permission]] = {}
        grants[(account_owner_person_id, "account_owner")] = ROLE_DEFAULT_PERMISSIONS[
            "account_owner"
        ]
        for person_id in primary_ids:
            grants[(person_id, "primary_subject")] = ROLE_DEFAULT_PERMISSIONS[
                "primary_subject"
            ]
        for person_id, role in roles:
            if role not in ALL_BINDING_ROLES:
                raise RoleConstraintError(f"unknown binding role {role!r}")
            if role in ("account_owner", "primary_subject"):
                raise RoleConstraintError(
                    f"{role} is derived from owner/subject declarations, not extra roles"
                )
            if person_id in primary_ids and role == "guardian":
                raise RoleConstraintError("a primary subject cannot be their own guardian")
            if (person_id, role) not in grants:
                if not await self._store.person_exists(person_id):
                    raise IdentityNotFoundError(f"person {person_id} does not exist")
                grants[(person_id, role)] = ROLE_DEFAULT_PERMISSIONS[role]
        if role_permissions:
            for key, permissions in role_permissions.items():
                if key not in grants:
                    raise RoleConstraintError(f"role grant {key} is not declared")
                unknown = permissions - ALL_PERMISSIONS
                if unknown:
                    raise RoleConstraintError(f"unknown permissions {sorted(unknown)!r}")
                role = key[1]
                defaults = ROLE_DEFAULT_PERMISSIONS[role]
                if not permissions <= defaults:
                    raise RoleConstraintError(
                        f"role {role} permissions must be a subset of its default "
                        "permissions; widening is rejected (payer/admin never "
                        "derives content.read or private memory)"
                    )
                grants[key] = permissions
        subject_categories: dict[str, SubjectCategory] = {}
        for person_id in primary_ids:
            person = await self._store.get_person(person_id)
            if person is not None:
                subject_categories[person_id] = person.subject_category
        self._validate_mode(
            declared_mode=declared_mode,
            account_owner_person_id=account_owner_person_id,
            primary_ids=primary_ids,
            subject_categories=subject_categories,
            grants=grants,
            family_space_id=family_space_id,
        )
        await self._validate_mode_relationships(
            declared_mode=declared_mode,
            account_owner_person_id=account_owner_person_id,
            primary_ids=primary_ids,
            grants=grants,
            now=now,
        )
        binding_id = _new_id()
        role_rows = tuple(
            DeviceBindingRole(
                binding_id=binding_id,
                person_id=person_id,
                role=role,
                permissions=permissions,
                granted_at=now,
            )
            for (person_id, role), permissions in sorted(grants.items())
        )
        return DeviceBinding(
            binding_id=binding_id,
            device_id=device_id,
            declared_mode=declared_mode,
            account_owner_person_id=account_owner_person_id,
            primary_subject_ids=primary_ids,
            binding_version=1,  # store assigns the real monotonic version
            status="active",
            reason=reason,
            family_space_id=family_space_id,
            supersedes_binding_id=supersedes_binding_id,
            valid_from=valid_from,
            valid_until=valid_until,
            roles=role_rows,
            service_profile_version=service_profile_version,
            policy_bundle_version=policy_bundle_version,
            consent_snapshot_id=consent_snapshot_id,
            persona_assignment_id=persona_assignment_id,
            created_at=now,
        )

    @staticmethod
    def _validate_mode(
        *,
        declared_mode: DeviceDeclaredMode,
        account_owner_person_id: str,
        primary_ids: tuple[str, ...],
        subject_categories: dict[str, SubjectCategory],
        grants: dict[tuple[str, BindingRole], frozenset[Permission]],
        family_space_id: str | None,
    ) -> None:
        guardian_ids = {
            person_id for (person_id, role) in grants if role == "guardian"
        }
        device_admin_ids = {
            person_id for (person_id, role) in grants if role == "device_admin"
        }
        emergency_ids = {
            person_id for (person_id, role) in grants if role == "emergency_contact"
        }
        if declared_mode == "parent_for_child":
            if any(
                subject_categories.get(person_id) == "adult"
                for person_id in primary_ids
            ):
                raise ModeConstraintError(
                    "parent_for_child cannot have an adult primary subject"
                )
            if not guardian_ids:
                raise ModeConstraintError(
                    "parent_for_child requires at least one guardian role"
                )
        elif declared_mode == "self_use":
            if len(primary_ids) != 1:
                raise ModeConstraintError("self_use requires exactly one primary subject")
            if primary_ids[0] != account_owner_person_id:
                raise ModeConstraintError(
                    "self_use requires the primary subject to be the account owner"
                )
            if guardian_ids:
                raise ModeConstraintError("self_use cannot declare a guardian")
        elif declared_mode == "child_for_parent":
            if len(primary_ids) != 1:
                raise ModeConstraintError(
                    "child_for_parent requires exactly one primary subject"
                )
            if primary_ids[0] == account_owner_person_id:
                raise ModeConstraintError(
                    "child_for_parent must separate the payer/administrator from the "
                    "primary subject"
                )
            if subject_categories.get(primary_ids[0]) == "minor":
                raise ModeConstraintError(
                    "child_for_parent cannot have a minor primary subject"
                )
            if not device_admin_ids or device_admin_ids == set(primary_ids):
                raise ModeConstraintError(
                    "child_for_parent requires a device_admin distinct from the "
                    "primary subject"
                )
            if not emergency_ids:
                raise ModeConstraintError(
                    "child_for_parent requires at least one emergency contact"
                )
        elif declared_mode == "family_shared":
            if not family_space_id:
                raise ModeConstraintError(
                    "family_shared requires a family_space_id"
                )
        else:
            raise ModeConstraintError(f"unknown declared mode {declared_mode!r}")

    async def _validate_mode_relationships(
        self,
        *,
        declared_mode: DeviceDeclaredMode,
        account_owner_person_id: str,
        primary_ids: tuple[str, ...],
        grants: dict[tuple[str, BindingRole], frozenset[Permission]],
        now: datetime,
    ) -> None:
        """Binding roles must be backed by exact active/confirmed
        relationships; a known person_id alone can never grant a role
        (acceptance 2, PR-05)."""
        if declared_mode == "self_use":
            return

        async def _verified(
            relation_type: str,
            source: str,
            target: str,
            *,
            role_label: str,
        ) -> None:
            if not await self._store.has_active_relationship(
                source_person_id=source,
                target_person_id=target,
                relation_type=relation_type,
                at=now,
            ):
                raise ModeConstraintError(
                    f"{role_label} requires an active confirmed "
                    f"{relation_type} relationship "
                    f"({source} -> {target})"
                )

        subjects = set(primary_ids)
        guardian_ids = {
            person_id for (person_id, role) in grants if role == "guardian"
        }
        admin_ids = {
            person_id for (person_id, role) in grants if role == "device_admin"
        }
        emergency_ids = {
            person_id for (person_id, role) in grants if role == "emergency_contact"
        }
        member_ids = {
            person_id for (person_id, role) in grants if role == "member"
        }

        if declared_mode == "parent_for_child":
            owner_set = {account_owner_person_id}
            for guardian_id in guardian_ids:
                for subject_id in subjects:
                    await _verified(
                        "guardian_of",
                        guardian_id,
                        subject_id,
                        role_label="guardian",
                    )
            for admin_id in admin_ids - owner_set:
                for subject_id in subjects:
                    await _verified(
                        "guardian_of",
                        admin_id,
                        subject_id,
                        role_label="device_admin",
                    )
            for emergency_id in emergency_ids:
                emergency_verified = False
                for subject_id in subjects:
                    if await self._store.has_active_relationship(
                        source_person_id=emergency_id,
                        target_person_id=subject_id,
                        relation_type="emergency_contact_for",
                        at=now,
                    ) or await self._store.has_active_relationship(
                        source_person_id=subject_id,
                        target_person_id=emergency_id,
                        relation_type="emergency_contact_for",
                        at=now,
                    ):
                        emergency_verified = True
                        break
                if not emergency_verified:
                    raise ModeConstraintError(
                        "parent_for_child emergency_contact requires an active "
                        "confirmed emergency_contact_for relationship with a "
                        f"primary subject ({emergency_id})"
                    )
        elif declared_mode == "child_for_parent":
            for subject_id in subjects:
                for admin_id in admin_ids:
                    child_of = await self._store.has_active_relationship(
                        source_person_id=admin_id,
                        target_person_id=subject_id,
                        relation_type="child_of",
                        at=now,
                    )
                    parent_of = await self._store.has_active_relationship(
                        source_person_id=subject_id,
                        target_person_id=admin_id,
                        relation_type="parent_of",
                        at=now,
                    )
                    if not (child_of or parent_of):
                        raise ModeConstraintError(
                            "child_for_parent device_admin requires an active "
                            f"confirmed child_of/parent_of relationship "
                            f"({admin_id} <-> {subject_id})"
                        )
                for emergency_id in emergency_ids:
                    emergency_verified = False
                    for subject_id in subjects:
                        if await self._store.has_active_relationship(
                            source_person_id=emergency_id,
                            target_person_id=subject_id,
                            relation_type="emergency_contact_for",
                            at=now,
                        ) or await self._store.has_active_relationship(
                            source_person_id=subject_id,
                            target_person_id=emergency_id,
                            relation_type="emergency_contact_for",
                            at=now,
                        ):
                            emergency_verified = True
                            break
                    if not emergency_verified:
                        raise ModeConstraintError(
                            "child_for_parent emergency_contact requires an "
                            "active confirmed emergency_contact_for "
                            f"relationship with a primary subject "
                            f"({emergency_id})"
                        )
        elif declared_mode == "family_shared":
            for member_id in member_ids - {account_owner_person_id}:
                if member_id in subjects:
                    continue
                family_verified = False
                for anchor_id in subjects | {account_owner_person_id}:
                    if await self._store.has_active_relationship(
                        source_person_id=member_id,
                        target_person_id=anchor_id,
                        relation_type="family_member_of",
                        at=now,
                    ) or await self._store.has_active_relationship(
                        source_person_id=anchor_id,
                        target_person_id=member_id,
                        relation_type="family_member_of",
                        at=now,
                    ):
                        family_verified = True
                        break
                if not family_verified:
                    raise ModeConstraintError(
                        "family_shared member requires an active confirmed "
                        f"family_member_of relationship ({member_id})"
                    )

    async def _require_active_binding(
        self,
        device_id: str,
        now: datetime,
        actor_person_id: str | None = None,
    ) -> DeviceBinding:
        binding = await self._store.get_active_binding(
            device_id, now, actor_person_id=actor_person_id
        )
        if binding is None:
            raise IdentityNotFoundError(
                f"device {device_id} has no active binding"
            )
        return binding

    async def _require_permission(
        self, binding: DeviceBinding, actor_person_id: str, permission: str
    ) -> None:
        manifest = manifest_from_binding(binding)
        if not has_permission(manifest, actor_person_id, permission):
            raise IdentityAccessDeniedError(
                f"person {actor_person_id} lacks {permission} on binding "
                f"{binding.binding_id}"
            )

    def _binding_events(
        self,
        *,
        action: str,
        manifest: BindingManifest,
        actor_person_id: str | None,
        timestamp: datetime,
        extra: dict[str, object] | None = None,
    ) -> tuple[AuditEvent, OutboxEvent]:
        """Build the audit + outbox pair for a binding mutation."""
        payload: dict[str, object] = manifest.to_dict()
        if extra:
            payload.update(extra)
        event_id = _new_id()
        audit = AuditEvent(
                event_id=event_id,
                action=action,
                actor_person_id=actor_person_id,
                subject_person_id=manifest.primary_subject_ids[0]
                if manifest.primary_subject_ids
                else None,
                person_id=None,
                device_id=manifest.device_id,
                binding_id=manifest.binding_id,
                relationship_id=None,
                payload=payload,
                created_at=timestamp,
            )
        outbox = OutboxEvent(
            outbox_id=_new_id(),
            event_id=event_id,
            topic=_OUTBOX_TOPIC_BY_ACTION[action],
            payload=payload,
            created_at=timestamp,
        )
        return audit, outbox

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


_OUTBOX_TOPIC_BY_ACTION = {
    "binding.create": "identity.binding.created",
    "binding.supersede": "identity.binding.superseded",
    "binding.transfer": "identity.binding.transferred",
    "binding.revoke": "identity.binding.revoked",
    "binding.expire": "identity.binding.expired",
}
