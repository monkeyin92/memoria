"""Application facade for the identity domain.

``IdentityService`` is the only entry point Control API needs: it executes
create / supersede / transfer / revoke / get-active and returns serializable
``BindingManifest`` objects, appending audit and transactional-outbox events
for every mutation.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

from services.consent.binding_snapshot import (
    BindingConsentCommand,
    BindingConsentRole,
    BindingRelationshipEvidence,
)
from services.identity.account_lifecycle import IdentityAccountLifecycleMixin
from services.identity.authority import (
    ConsentAuthorityUnavailableError,
    ConsentSnapshotResolver,
    RejectingConsentSnapshotResolver,
    RejectingTransferEvidenceVerifier,
    TransferEvidenceVerifier,
)
from services.identity.binding_roles import parent_emergency_contact_verified
from services.identity.domain import (
    ALL_BINDING_ROLES,
    ALL_PERMISSIONS,
    ROLE_DEFAULT_PERMISSIONS,
    AgeBand,
    AgeEvidenceError,
    AgeEvidenceStatus,
    BindingManifest,
    BindingReason,
    BindingRole,
    BindingVersionConflictError,
    DeviceBinding,
    DeviceBindingRole,
    DeviceDeclaredMode,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    ModeConstraintError,
    Permission,
    PersonSubject,
    Relationship,
    RoleConstraintError,
    SubjectCategory,
    derive_subject_category,
    has_permission,
    manifest_from_binding,
    validate_age_declaration,
    validate_manifest_wire,
)
from services.identity.personas import IdentityPersonaMixin
from services.identity.relationships import REDACTED_DISPLAY_NAME as REDACTED_DISPLAY_NAME
from services.identity.relationships import IdentityRelationshipMixin
from services.identity.repository import AuditEvent, IdentityStore, OutboxEvent
from services.identity.service_helpers import (
    _canonical_registration_result,
    _new_id,
    _now,
)
from services.identity.subject_release import BindingRevocation
from services.identity.transfers import IdentityTransferMixin


class IdentityService(IdentityAccountLifecycleMixin, IdentityPersonaMixin, IdentityTransferMixin, IdentityRelationshipMixin):
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
        expected_binding_id: str | None = None,
    ) -> BindingManifest:
        """Issue the next binding version (mode / subject / member change).

        ``expected_binding_id`` is an optional compare-and-set for callers
        that derived the new field values (subject list, roles, permissions)
        from a previously read version.  If a concurrent supersede already
        produced a newer version, this call fails with
        :class:`BindingVersionConflictError` instead of silently writing the
        stale derivation over the other writer's fields.  The check runs
        against the same read whose version is superseded, and the store
        re-verifies under its per-device lock that the superseded row is still
        the active one, so a lost update cannot slip between the two.
        """
        timestamp = _now(now)
        current = await self._require_active_binding(
            device_id, timestamp, actor_person_id=actor_person_id
        )
        await self._require_permission(current, actor_person_id, "binding.manage")
        if expected_binding_id is not None and current.binding_id != expected_binding_id:
            raise BindingVersionConflictError(
                "the active binding changed after it was read; re-read the "
                "active binding and retry"
            )
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
    ) -> BindingRevocation:
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
        return await self._store.revoke_binding(
            current.binding_id,
            at=timestamp,
            audit_event=audit,
            outbox_event=outbox,
            actor_person_id=actor_person_id,
        )

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
        (acceptance 2, PR-05).

        One exception is named explicitly: a ``parent_for_child`` guardian
        role may rest on a **declared** ``guardian_of`` relationship when the
        subject has no account of their own.  Such a relationship is still
        ``pending`` with only the guardian endpoint confirmed, so the binding
        records a declaration.  It never becomes verified guardianship here,
        and guardian capabilities (consent, notification, memory retention,
        session admission) stay gated on the guardian authority that requires
        the subject's own confirmation.
        """
        if declared_mode == "self_use":
            return

        async def _verified(
            relation_type: str,
            source: str,
            target: str,
            *,
            role_label: str,
            accept_declaration: bool = False,
        ) -> None:
            if await self._store.has_active_relationship(
                source_person_id=source,
                target_person_id=target,
                relation_type=relation_type,
                at=now,
            ):
                return
            if accept_declaration and await self._store.has_source_confirmed_relationship(
                source_person_id=source,
                target_person_id=target,
                relation_type=relation_type,
                at=now,
            ):
                return
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
                # A bare one-sided declaration is only a binding-scoped fact:
                # it counts when the declarant is the binding's account owner
                # (the production ``subject_draft`` write shape) OR when the
                # same guardian holds an ACTIVE two-party ``guardian_of``
                # relationship to the subject (the account-holder family
                # shape, e.g. a distinct guardian role alongside the owner).
                # A pending-only self-declaration from a non-owner stays a
                # notification-candidate at most — it never satisfies the
                # binding role.  Without this, any login that learns a
                # child's person id could self-declare guardian_of and take
                # the guardian role.
                if guardian_id != account_owner_person_id:
                    verified_guardian = False
                    for subject_id in subjects:
                        if await self._store.has_active_relationship(
                            source_person_id=guardian_id,
                            target_person_id=subject_id,
                            relation_type="guardian_of",
                            at=now,
                        ):
                            verified_guardian = True
                            break
                    if not verified_guardian:
                        raise ModeConstraintError(
                            "parent_for_child guardian must be the binding "
                            f"account owner or hold a verified guardian_of "
                            f"relationship ({guardian_id})"
                        )
                for subject_id in subjects:
                    await _verified(
                        "guardian_of",
                        guardian_id,
                        subject_id,
                        role_label="guardian",
                        accept_declaration=True,
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
                if not await parent_emergency_contact_verified(
                    self._store,
                    emergency_id=emergency_id,
                    account_owner_id=account_owner_person_id,
                    guardian_ids=guardian_ids,
                    subject_ids=subjects,
                    at=now,
                ):
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
                    # An elderly parent with no account cannot confirm an
                    # endpoint; the adult child's binding-attested
                    # delegate_for stands in (2026-09-25).
                    delegate = await self._store.has_active_relationship(
                        source_person_id=admin_id,
                        target_person_id=subject_id,
                        relation_type="delegate_for",
                        at=now,
                    )
                    if not (child_of or parent_of or delegate):
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
                        ) or await self._store.has_active_relationship(
                            source_person_id=emergency_id,
                            target_person_id=subject_id,
                            relation_type="delegate_for",
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


_OUTBOX_TOPIC_BY_ACTION = {
    "binding.create": "identity.binding.created",
    "binding.supersede": "identity.binding.superseded",
    "binding.transfer": "identity.binding.transferred",
    "binding.revoke": "identity.binding.revoked",
    "binding.expire": "identity.binding.expired",
}
