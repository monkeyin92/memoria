"""Control-plane composition for subject resolution and Runtime Profiles.

The HTTP layer delegates here so identity snapshots, persona assignment,
policy evaluation, signing, and session-epoch fencing stay one deep module.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    ALL_CAPABILITY_VALUES,
    RuntimeProfileSignedV2,
)

from services.identity.domain import (
    BindingManifest,
    IdentityAccessDeniedError,
    IdentityNotFoundError,
    PersonSubject,
)
from services.identity.service import IdentityService
from services.policy.engine import Capability, PolicyDecision, PolicyEngine
from services.session_runtime.profile_service import (
    InMemoryRuntimeAuthority,
    PersonaAssignment,
    RuntimeProfile,
    RuntimeProfileService,
    StartSessionCommand,
    SubjectFacts,
    SwitchSubjectCommand,
    runtime_profile_wire_payload,
)
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PostgresSessionRuntimeService,
    StartPersistentSessionCommand,
    SwitchPersistentSubjectCommand,
)
from services.session_runtime.subject_resolver import BindingSnapshot

_REQUESTED_CAPABILITIES: tuple[Capability, ...] = tuple(
    value.value for value in ALL_CAPABILITY_VALUES
)


def _canonical_runtime_profile_id() -> str:
    return f"rp_{uuid.uuid4()}"


class SubjectNotBindingMemberError(IdentityAccessDeniedError):
    """The requested active subject has no role in the device binding."""


class SubjectSwitchForbiddenError(IdentityAccessDeniedError):
    """The actor holds a binding role but lacks the switch/confirm authority."""


class PolicyActorMismatchError(IdentityAccessDeniedError):
    """A decision was requested for a profile whose actor is not the requester.

    Policy decisions are fenced to the signed profile's own actor until a
    verifiable, capability-level delegation exists. A binding member must not
    borrow another subject's identity to evaluate policy.
    """


@dataclass(frozen=True, slots=True)
class SubjectResolutionResult:
    resolution: Literal["confirmed", "confirmation_required"]
    profile: RuntimeProfile | RuntimeProfileSignedV2
    candidate_subjects: list[dict[str, object]]


class MultiSubjectRuntimeControl:
    """Translate authoritative bindings into fenced session profiles."""

    def __init__(
        self,
        *,
        identity: IdentityService,
        policy: PolicyEngine,
        signing_key: bytes,
    ) -> None:
        self.identity = identity
        self.authority = InMemoryRuntimeAuthority()
        self.profiles = RuntimeProfileService(
            authority=self.authority,
            policy=policy,
            signing_key=signing_key,
            profile_id_factory=_canonical_runtime_profile_id,
        )
        # Single-process serialization for profile issuance, subject switches
        # and decisions. A distributed deployment must replace this with a
        # session-scoped advisory lock or revision projection.
        self._lock = asyncio.Lock()

    async def active_manifest(
        self,
        device_id: str,
        *,
        now: datetime,
    ) -> BindingManifest:
        async with self._lock:
            return await self._active_manifest(device_id, now=now)

    async def require_binding_member(
        self,
        *,
        device_id: str,
        user_id: str,
        now: datetime,
    ) -> BindingManifest:
        """Fail closed unless the user holds a role on the device binding."""
        async with self._lock:
            manifest = await self._active_manifest(device_id, now=now)
            if not self._is_binding_member(manifest, user_id):
                raise IdentityAccessDeniedError(
                    f"user {user_id} has no role on device {device_id}"
                )
            return manifest

    async def _active_manifest(
        self,
        device_id: str,
        *,
        now: datetime,
    ) -> BindingManifest:
        manifest = await self.identity.get_active_manifest(device_id, now)
        if manifest is None:
            raise IdentityNotFoundError(f"device {device_id} has no active binding")
        await self._refresh_authority(manifest)
        return manifest

    async def ensure_profile(
        self,
        *,
        device_id: str,
        session_id: str | None,
        actor_id: str,
        now: datetime,
        multiple_speakers: bool = False,
        offline: bool = False,
    ) -> RuntimeProfile:
        async with self._lock:
            return await self._ensure_profile_unlocked(
                device_id=device_id,
                session_id=session_id,
                actor_id=actor_id,
                now=now,
                multiple_speakers=multiple_speakers,
                offline=offline,
            )

    async def _ensure_profile_unlocked(
        self,
        *,
        device_id: str,
        session_id: str | None,
        actor_id: str,
        now: datetime,
        multiple_speakers: bool,
        offline: bool,
    ) -> RuntimeProfile:
        manifest = await self._active_manifest(device_id, now=now)
        if not self._is_binding_member(manifest, actor_id):
            raise IdentityAccessDeniedError(
                f"user {actor_id} has no role on device {device_id}"
            )
        resolved_session_id = session_id or self.default_session_id(device_id)
        current = self.authority.current_profile(resolved_session_id)
        if current is not None:
            current_valid = (
                current.binding_id == manifest.binding_id
                and current.binding_version == manifest.binding_version
                and self.profiles.verify(current, now=now)
            )
            if current_valid and not multiple_speakers and not offline:
                return current
            return self.profiles.switch_subject(
                SwitchSubjectCommand(
                    session_id=resolved_session_id,
                    actor_id=current.actor_id,
                    candidates=(),
                    requested_capabilities=_REQUESTED_CAPABILITIES,
                    now=now,
                    relationship_roles=self._roles(manifest, current.actor_id),
                    multiple_speakers=multiple_speakers,
                    offline=offline,
                )
            )
        return self.profiles.start(
            StartSessionCommand(
                session_id=resolved_session_id,
                device_id=device_id,
                actor_id=actor_id,
                candidates=(),
                requested_capabilities=_REQUESTED_CAPABILITIES,
                now=now,
                relationship_roles=self._roles(manifest, actor_id),
                multiple_speakers=multiple_speakers,
                offline=offline,
            )
        )

    async def resolve_subject(
        self,
        *,
        device_id: str,
        session_id: str | None,
        actor_id: str,
        now: datetime,
        multiple_speakers: bool = False,
        offline: bool = False,
        client_claimed_person_id: str | None = None,
    ) -> SubjectResolutionResult:
        """Resolve the current subject without trusting any client claim.

        ``client_claimed_person_id`` is only a candidate hint (ordering). It is
        never passed to the resolver as proof, so it cannot issue a confirmed
        profile. When the claimed person differs from an existing confirmed
        subject, only an actor with switch authority over the claimed target
        may transition the session to a fresh unknown-safe profile (epoch+1,
        previous subject's capabilities paused); claims without authority are
        ignored so they cannot cut off another person's session.
        """
        async with self._lock:
            manifest = await self._active_manifest(device_id, now=now)
            if not self._is_binding_member(manifest, actor_id):
                raise IdentityAccessDeniedError(
                    f"user {actor_id} has no role on device {device_id}"
                )
            resolved_session_id = session_id or self.default_session_id(device_id)
            current = self.authority.current_profile(resolved_session_id)
            candidates = await self._candidates(manifest, hint=client_claimed_person_id)
            current_confirmed = (
                current is not None
                and current.active_subject_id is not None
                and current.speaker_state == "confirmed"
                and current.binding_id == manifest.binding_id
                and current.binding_version == manifest.binding_version
                and self.profiles.verify(current, now=now)
                and not multiple_speakers
                and not offline
            )
            if current_confirmed and current is not None:
                claim = client_claimed_person_id
                if claim is not None and claim != current.active_subject_id:
                    if self._can_switch_subject(manifest, actor_id, claim):
                        profile = await self._transition_to_unknown_safe(
                            session_id=resolved_session_id,
                            manifest=manifest,
                            current=current,
                            actor_id=actor_id,
                            now=now,
                        )
                        return SubjectResolutionResult(
                            resolution="confirmation_required",
                            profile=profile,
                            candidate_subjects=candidates,
                        )
                    # Unauthorized claim is ignored: the session remains
                    # confirmed, so the response must not start a confirmation
                    # flow that the state cannot satisfy.
                    return SubjectResolutionResult(
                        resolution="confirmed",
                        profile=current,
                        candidate_subjects=candidates,
                    )
                return SubjectResolutionResult(
                    resolution="confirmed",
                    profile=current,
                    candidate_subjects=candidates,
                )
            profile = await self._ensure_profile_unlocked(
                device_id=device_id,
                session_id=resolved_session_id,
                actor_id=actor_id,
                now=now,
                multiple_speakers=multiple_speakers,
                offline=offline,
            )
            return SubjectResolutionResult(
                resolution="confirmation_required",
                profile=profile,
                candidate_subjects=candidates,
            )

    async def _transition_to_unknown_safe(
        self,
        *,
        session_id: str,
        manifest: BindingManifest,
        current: RuntimeProfile,
        actor_id: str,
        now: datetime,
    ) -> RuntimeProfile:
        """Issue the next epoch as unknown-safe with the subject unconfirmed.

        The actor who initiated the authorized transition becomes the profile's
        actor so the decision fence stays consistent; the subject remains
        unconfirmed (null) and the previous confirmed profile becomes stale,
        pausing its private-memory and generation capabilities until a trusted
        confirmation.
        """
        return self.profiles.switch_subject(
            SwitchSubjectCommand(
                session_id=session_id,
                actor_id=actor_id,
                candidates=(),
                requested_capabilities=_REQUESTED_CAPABILITIES,
                now=now,
                relationship_roles=self._roles(manifest, actor_id),
            )
        )

    async def switch_subject(
        self,
        *,
        session_id: str,
        subject_id: str,
        actor_id: str,
        now: datetime,
    ) -> RuntimeProfile:
        async with self._lock:
            current = self.authority.current_profile(session_id)
            if current is None:
                raise LookupError("active runtime profile is unavailable")
            manifest = await self._active_manifest(current.device_id, now=now)
            if not self._is_binding_member(manifest, actor_id):
                raise IdentityAccessDeniedError(
                    f"user {actor_id} has no role on device {current.device_id}"
                )
            if subject_id not in self._member_subject_ids(manifest):
                raise SubjectNotBindingMemberError(
                    "subject is not an active binding member"
                )
            if not self._can_switch_subject(manifest, actor_id, subject_id):
                raise SubjectSwitchForbiddenError(
                    f"{actor_id} cannot confirm subject {subject_id}"
                )
            return self.profiles.switch_subject(
                SwitchSubjectCommand(
                    session_id=session_id,
                    actor_id=subject_id,
                    candidates=(),
                    requested_capabilities=_REQUESTED_CAPABILITIES,
                    now=now,
                    relationship_roles=self._roles(manifest, subject_id),
                    app_claimed_subject_id=subject_id,
                )
            )

    async def decide(
        self,
        *,
        runtime_profile_id: str,
        capability: Capability,
        actor_id: str,
        data_classification: str,
        safety_state: str,
        now: datetime,
    ) -> PolicyDecision:
        async with self._lock:
            profile = self.authority.profile_by_id(runtime_profile_id)
            if profile is None:
                raise LookupError("runtime profile is unavailable")
            manifest = await self._active_manifest(profile.device_id, now=now)
            if not self._is_binding_member(manifest, actor_id):
                raise IdentityAccessDeniedError(
                    f"user {actor_id} has no role on device {profile.device_id}"
                )
            # Fail closed before any policy evaluation: the requester must be
            # the signed profile's own actor. Verifiable capability-level
            # delegation does not exist yet, so nothing else is inferred.
            self.profiles.require_valid(profile, now=now)
            if actor_id != profile.actor_id:
                raise PolicyActorMismatchError(
                    f"user {actor_id} cannot request decisions for actor "
                    f"{profile.actor_id}"
                )
            return self.profiles.decide(
                profile,
                capability=capability,
                now=now,
                relationship_roles=self._roles(manifest, profile.actor_id),
                data_classification=data_classification,
                safety_state=safety_state,
            )

    def current_profile(self, session_id: str) -> RuntimeProfile | None:
        return self.authority.current_profile(session_id)

    def profile_by_id(self, runtime_profile_id: str) -> RuntimeProfile | None:
        return self.authority.profile_by_id(runtime_profile_id)

    async def _candidates(
        self,
        manifest: BindingManifest,
        *,
        hint: str | None,
    ) -> list[dict[str, object]]:
        member_ids = self._member_subject_ids(manifest)
        if hint is not None and hint in member_ids:
            ordered: tuple[str, ...] = (
                hint,
                *(person_id for person_id in member_ids if person_id != hint),
            )
        else:
            ordered = member_ids
        result: list[dict[str, object]] = []
        for person_id in ordered:
            person = await self.identity.get_person(person_id)
            result.append(
                {
                    "person_id": person.person_id,
                    "display_name": person.display_name,
                    "confidence": 0.0,
                }
            )
        return result

    @staticmethod
    def default_session_id(device_id: str) -> str:
        return f"device-control:{device_id}"

    @staticmethod
    def serialize_profile(profile: RuntimeProfile) -> dict[str, object]:
        payload = runtime_profile_wire_payload(profile)
        payload["signature"] = profile.signature
        return payload

    @staticmethod
    def serialize_decision(decision: PolicyDecision) -> dict[str, object]:
        return {
            "effect": decision.effect,
            "reason_code": decision.reason_code,
            "obligations": [
                obligation.code if hasattr(obligation, "code") else obligation
                for obligation in decision.obligations
            ],
            "policy_version": decision.policy_version,
            "receipt_id": decision.receipt_id,
            "context_hash": decision.context_hash,
            "created_at": (
                decision.created_at.isoformat() if decision.created_at is not None else None
            ),
            "expires_at": (
                decision.expires_at.isoformat() if decision.expires_at is not None else None
            ),
        }

    async def _refresh_authority(self, manifest: BindingManifest) -> None:
        member_ids = self._member_subject_ids(manifest)
        self.authority.upsert_binding(
            BindingSnapshot(
                binding_id=manifest.binding_id,
                device_id=manifest.device_id,
                binding_version=manifest.binding_version,
                declared_mode=manifest.declared_mode,
                primary_subject_ids=manifest.primary_subject_ids,
                member_subject_ids=member_ids,
            )
        )
        for person_id in member_ids:
            person = await self.identity.get_person(person_id)
            self.authority.upsert_subject(self._subject_facts(person))
        assignment_id = manifest.persona_assignment_id or "starlight:v1"
        persona_id, _, version_text = assignment_id.partition(":v")
        persona_version = int(version_text) if version_text.isdigit() else 1
        self.authority.upsert_persona(
            PersonaAssignment(
                assignment_id=assignment_id,
                persona_id=persona_id or "starlight",
                persona_version=persona_version,
                relationship_stage="new",
            )
        )

    @staticmethod
    def _subject_facts(person: PersonSubject) -> SubjectFacts:
        revision = max(1, int(person.updated_at.timestamp() * 1_000_000))
        return SubjectFacts(
            subject_id=person.person_id,
            category=person.subject_category,
            age_band=person.age_band,
            revision=revision,
        )

    @staticmethod
    def _member_subject_ids(manifest: BindingManifest) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*manifest.primary_subject_ids, *manifest.member_ids)))

    @staticmethod
    def _is_binding_member(manifest: BindingManifest, user_id: str) -> bool:
        return user_id in {
            manifest.account_owner_id,
            *manifest.device_admin_ids,
            *manifest.guardian_ids,
            *manifest.member_ids,
            *manifest.primary_subject_ids,
        }

    @staticmethod
    def _can_switch_subject(
        manifest: BindingManifest,
        actor_id: str,
        subject_id: str,
    ) -> bool:
        """Action-level authority: who may confirm whom on this device."""
        if actor_id == subject_id:
            return True
        if actor_id in {manifest.account_owner_id, *manifest.device_admin_ids}:
            return True
        return (
            actor_id in manifest.guardian_ids
            and subject_id in manifest.primary_subject_ids
        )

    @staticmethod
    def _roles(manifest: BindingManifest, actor_id: str) -> frozenset[str]:
        roles: set[str] = {
            role.role for role in manifest.roles if role.person_id == actor_id
        }
        if actor_id in manifest.primary_subject_ids:
            roles.add("self")
        return frozenset(roles)


class PostgresMultiSubjectRuntimeControl:
    """Production Control facade over the single PostgreSQL Session authority."""

    def __init__(
        self,
        *,
        identity: IdentityService,
        sessions: PostgresSessionRuntimeService,
    ) -> None:
        self.identity = identity
        self.sessions = sessions

    async def active_manifest(
        self,
        device_id: str,
        *,
        now: datetime,
    ) -> BindingManifest:
        manifest = await self.identity.get_active_manifest(device_id, now)
        if manifest is None:
            raise IdentityNotFoundError(f"device {device_id} has no active binding")
        return manifest

    async def require_binding_member(
        self,
        *,
        device_id: str,
        user_id: str,
        now: datetime,
    ) -> BindingManifest:
        manifest = await self.active_manifest(device_id, now=now)
        if not MultiSubjectRuntimeControl._is_binding_member(manifest, user_id):
            raise IdentityAccessDeniedError(
                f"user {user_id} has no role on device {device_id}"
            )
        return manifest

    async def ensure_profile(
        self,
        *,
        device_id: str,
        session_id: str | None,
        actor_id: str,
        now: datetime,
        multiple_speakers: bool = False,
        offline: bool = False,
    ) -> RuntimeProfileSignedV2:
        manifest = await self.require_binding_member(
            device_id=device_id,
            user_id=actor_id,
            now=now,
        )
        resolved_session_id = session_id or self.default_session_id(device_id)
        try:
            current, _context = await self.sessions.current(
                actor_id=actor_id,
                session_id=resolved_session_id,
                now=now,
            )
        except PersistentSessionNotFound:
            current = await self.sessions.start(
                StartPersistentSessionCommand(
                    session_id=resolved_session_id,
                    actor_id=actor_id,
                    device_id=device_id,
                    expected_binding_version=manifest.binding_version,
                    idempotency_key=f"control-profile:{resolved_session_id}",
                    now=now,
                    requested_capabilities=_REQUESTED_CAPABILITIES,
                    multiple_speakers=multiple_speakers,
                    offline=offline,
                )
            )
            return current
        if current.device_id != device_id:
            raise PersistentSessionDenied("Session device does not match request")
        if (
            current.binding_id != manifest.binding_id
            or current.binding_version != manifest.binding_version
        ):
            raise PersistentSessionDenied("Session binding is stale")
        if (multiple_speakers or offline) and current.actor_id == actor_id:
            return await self.sessions.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=resolved_session_id,
                    actor_id=actor_id,
                    subject_id=None,
                    now=now,
                    requested_capabilities=_REQUESTED_CAPABILITIES,
                )
            )
        return current

    async def resolve_subject(
        self,
        *,
        device_id: str,
        session_id: str | None,
        actor_id: str,
        now: datetime,
        multiple_speakers: bool = False,
        offline: bool = False,
        client_claimed_person_id: str | None = None,
    ) -> SubjectResolutionResult:
        manifest = await self.require_binding_member(
            device_id=device_id,
            user_id=actor_id,
            now=now,
        )
        resolved_session_id = session_id or self.default_session_id(device_id)
        profile = await self.ensure_profile(
            device_id=device_id,
            session_id=resolved_session_id,
            actor_id=actor_id,
            now=now,
            multiple_speakers=multiple_speakers,
            offline=offline,
        )
        candidates = await self._candidates(
            manifest,
            hint=client_claimed_person_id,
        )
        confirmed = (
            profile.active_subject_id is not None
            and profile.speaker_state.value == "confirmed"
            and not multiple_speakers
            and not offline
        )
        if confirmed:
            claim = client_claimed_person_id
            if claim is not None and claim != profile.active_subject_id:
                if MultiSubjectRuntimeControl._can_switch_subject(
                    manifest,
                    actor_id,
                    claim,
                ):
                    transitioned = await self.sessions.switch_subject(
                        SwitchPersistentSubjectCommand(
                            session_id=resolved_session_id,
                            actor_id=actor_id,
                            subject_id=None,
                            claimed_subject_id=claim,
                            now=now,
                            requested_capabilities=_REQUESTED_CAPABILITIES,
                        )
                    )
                    return SubjectResolutionResult(
                        resolution="confirmation_required",
                        profile=transitioned,
                        candidate_subjects=candidates,
                    )
                return SubjectResolutionResult(
                    resolution="confirmed",
                    profile=profile,
                    candidate_subjects=candidates,
                )
            return SubjectResolutionResult(
                resolution="confirmed",
                profile=profile,
                candidate_subjects=candidates,
            )
        return SubjectResolutionResult(
            resolution="confirmation_required",
            profile=profile,
            candidate_subjects=candidates,
        )

    async def switch_subject(
        self,
        *,
        session_id: str,
        subject_id: str,
        actor_id: str,
        now: datetime,
    ) -> RuntimeProfileSignedV2:
        return await self.sessions.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=session_id,
                actor_id=actor_id,
                subject_id=subject_id,
                now=now,
                requested_capabilities=_REQUESTED_CAPABILITIES,
            )
        )

    async def decide(
        self,
        *,
        runtime_profile_id: str,
        capability: Capability,
        actor_id: str,
        data_classification: str,
        safety_state: str,
        now: datetime,
    ) -> PolicyDecision:
        return await self.sessions.decide(
            runtime_profile_id=runtime_profile_id,
            capability=capability,
            actor_id=actor_id,
            data_classification=data_classification,
            safety_state=safety_state,
            now=now,
        )

    async def _candidates(
        self,
        manifest: BindingManifest,
        *,
        hint: str | None,
    ) -> list[dict[str, object]]:
        member_ids = MultiSubjectRuntimeControl._member_subject_ids(manifest)
        if hint is not None and hint in member_ids:
            ordered = (
                hint,
                *(item for item in member_ids if item != hint),
            )
        else:
            ordered = member_ids
        result: list[dict[str, object]] = []
        for person_id in ordered:
            person = await self.identity.get_person(person_id)
            result.append(
                {
                    "person_id": person.person_id,
                    "display_name": person.display_name,
                    "confidence": 0.0,
                }
            )
        return result

    async def tutor_profile(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> RuntimeProfileSignedV2 | None:
        try:
            profile, _context = await self.sessions.current(
                actor_id=actor_id,
                session_id=session_id,
                now=now,
            )
        except (PersistentSessionNotFound, PersistentSessionDenied):
            return None
        return profile if profile.actor_id == actor_id else None

    @staticmethod
    def default_session_id(device_id: str) -> str:
        return MultiSubjectRuntimeControl.default_session_id(device_id)

    @staticmethod
    def serialize_profile(
        profile: RuntimeProfile | RuntimeProfileSignedV2,
    ) -> dict[str, object]:
        if isinstance(profile, RuntimeProfileSignedV2):
            return cast(dict[str, object], profile.model_dump(mode="json"))
        return MultiSubjectRuntimeControl.serialize_profile(profile)

    @staticmethod
    def serialize_decision(decision: PolicyDecision) -> dict[str, object]:
        return MultiSubjectRuntimeControl.serialize_decision(decision)
