"""Issue signed, short-lived Runtime Profiles for one active subject epoch."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    PolicyObligationSpec,
    PurposeValue,
    ServiceModeValue,
    SpeakerStateValue,
    SubjectCategoryValue,
)

from services.policy.context import (
    canonical_purpose_for_capability,
    canonical_runtime_purpose_for_capability,
)
from services.policy.engine import (
    Capability,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
)
from services.session_runtime.service_mode_resolver import ServiceModeResolver
from services.session_runtime.subject_resolver import (
    BindingSnapshot,
    ResolveSubjectCommand,
    SubjectCandidate,
    SubjectResolver,
)

_MAX_PROFILE_HISTORY_PER_SESSION: int = 16
RUNTIME_PROFILE_PAYLOAD_SCHEMA: Final[str] = "runtime-profile-v2"

PROFILE_ISSUE_SESSION_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {
        "chat",
        "tutor",
        "english_practice",
        "memory_recall_private",
        "guardian_summary_view",
    }
)

# These capabilities only become meaningful once the caller supplies the real
# action/resource identity (for example a memory candidate, family proposal,
# payment, voice profile, notification, or transfer target).  Session start has
# none of those facts, so it must neither list them as allowed nor mint a
# reusable policy receipt.  The same-connection action authorizer evaluates
# them later with the actual resource fence.
PROFILE_ISSUE_DEFERRED_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {
        "memory_capture",
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "voice_profile_create",
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "payment",
        "raw_audio_retention",
        "model_training_contribution",
        "crisis_notification",
        "device_ownership_transfer",
    }
)

def canonical_profile_issue_purpose(capability: Capability) -> PurposeValue:
    """Delegate profile-time purpose resolution to Policy's authority.

    Generated action pairs retain their exact purpose.  Session-level and
    deferred non-action capabilities explicitly use the supported profile
    fallback; unknown values still fail closed in Policy.
    """
    try:
        return canonical_purpose_for_capability(capability)
    except ValueError:
        return canonical_purpose_for_capability(
            capability,
            fallback_purpose="runtime_profile_issue",
        )


def canonical_runtime_decision_purpose(capability: Capability) -> PurposeValue:
    """Bind every post-issuance capability to one semantic action purpose."""
    return canonical_runtime_purpose_for_capability(capability)


def runtime_profile_wire_payload(profile: RuntimeProfile) -> dict[str, object]:
    """The single canonical wire payload (without ``signature``) that is HMAC-signed.

    Profile issuance and the HTTP serializer must both use this exact shape so
    any consumer (Agent, firmware, H5) can rebuild the signature bytes from the
    public response. ``signature`` itself never enters the payload.
    """
    obligations = [
        obligation.model_dump(mode="json") for obligation in profile.obligations
    ]
    return {
        "signature_schema": RUNTIME_PROFILE_PAYLOAD_SCHEMA,
        "runtime_profile_id": profile.runtime_profile_id,
        "device_id": profile.device_id,
        "session_id": profile.session_id,
        "actor_id": profile.actor_id,
        "binding_id": profile.binding_id,
        "binding_version": profile.binding_version,
        "active_subject_id": profile.active_subject_id,
        "subject_revision": profile.subject_revision,
        "subject_category": profile.subject_category,
        "age_band": profile.age_band,
        "speaker_state": profile.speaker_state,
        "speaker_confidence": profile.speaker_confidence,
        "service_mode": profile.service_mode,
        "persona_assignment_id": profile.persona_assignment_id,
        "persona": {
            "persona_id": profile.persona_id,
            "version": profile.persona_version,
            "relationship_stage": profile.relationship_stage,
        },
        "policy_bundle_version": profile.policy_bundle_version,
        "capabilities": list(profile.capabilities),
        "obligations": obligations,
        "policy_receipt_ids": list(profile.policy_receipt_ids),
        "session_epoch": profile.session_epoch,
        "issued_at": profile.issued_at.isoformat(),
        "expires_at": profile.expires_at.isoformat(),
    }


def sign_runtime_profile_payload(
    payload: dict[str, object],
    *,
    signing_key: bytes,
) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(signing_key, encoded, hashlib.sha256).hexdigest()


def verify_runtime_profile_payload(
    payload: dict[str, object],
    *,
    signing_key: bytes,
) -> bool:
    """Verify a public wire payload (including its ``signature`` key)."""
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    if payload.get("signature_schema") != RUNTIME_PROFILE_PAYLOAD_SCHEMA:
        return False
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    expected = sign_runtime_profile_payload(unsigned, signing_key=signing_key)
    return hmac.compare_digest(signature, expected)


@dataclass(frozen=True, slots=True)
class SubjectFacts:
    subject_id: str
    category: SubjectCategoryValue
    age_band: AgeBandValue
    revision: int


@dataclass(frozen=True, slots=True)
class PersonaAssignment:
    assignment_id: str
    persona_id: str
    persona_version: int
    relationship_stage: str


@dataclass(frozen=True, slots=True)
class StartSessionCommand:
    session_id: str
    device_id: str
    actor_id: str
    candidates: tuple[SubjectCandidate, ...]
    requested_capabilities: tuple[Capability, ...]
    now: datetime
    relationship_roles: frozenset[str] = frozenset()
    consent_kinds: frozenset[str] = frozenset()
    app_claimed_subject_id: str | None = None
    multiple_speakers: bool = False
    offline: bool = False


@dataclass(frozen=True, slots=True)
class SwitchSubjectCommand:
    session_id: str
    actor_id: str
    candidates: tuple[SubjectCandidate, ...]
    requested_capabilities: tuple[Capability, ...]
    now: datetime
    relationship_roles: frozenset[str] = frozenset()
    consent_kinds: frozenset[str] = frozenset()
    app_claimed_subject_id: str | None = None
    multiple_speakers: bool = False
    offline: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    runtime_profile_id: str
    device_id: str
    session_id: str
    actor_id: str
    binding_id: str
    binding_version: int
    active_subject_id: str | None
    subject_revision: int
    subject_category: SubjectCategoryValue
    age_band: AgeBandValue
    speaker_state: SpeakerStateValue
    speaker_confidence: float | None
    service_mode: ServiceModeValue
    persona_assignment_id: str
    persona_id: str
    persona_version: int
    relationship_stage: str
    policy_bundle_version: str
    capabilities: tuple[Capability, ...]
    obligations: tuple[PolicyObligationSpec, ...]
    policy_receipt_ids: tuple[str, ...]
    session_epoch: int
    issued_at: datetime
    expires_at: datetime
    signature: str

    def __post_init__(self) -> None:
        """Keep the in-process signing input aligned with RuntimeProfile V2."""
        normalized: list[PolicyObligationSpec] = []
        seen_codes: set[str] = set()
        for raw in self.obligations:
            obligation = PolicyObligationSpec.model_validate(raw)
            code = obligation.code.value
            if code in seen_codes:
                raise ValueError(
                    f"runtime profile obligation code is duplicated: {code}"
                )
            seen_codes.add(code)
            normalized.append(obligation)
        object.__setattr__(self, "obligations", tuple(normalized))


class InMemoryRuntimeAuthority:
    """Local adapter used by tests and development composition."""

    def __init__(
        self,
        *,
        bindings: tuple[BindingSnapshot, ...] = (),
        subjects: tuple[SubjectFacts, ...] = (),
        personas: tuple[PersonaAssignment, ...] = (),
    ) -> None:
        self._bindings = {item.device_id: item for item in bindings}
        self._subjects = {item.subject_id: item for item in subjects}
        self._personas = personas
        self._profiles: dict[str, RuntimeProfile] = {}
        self._profile_history: dict[str, RuntimeProfile] = {}

    def active_binding(self, device_id: str) -> BindingSnapshot | None:
        return self._bindings.get(device_id)

    def upsert_binding(self, binding: BindingSnapshot) -> None:
        self._bindings[binding.device_id] = binding

    def subject(self, subject_id: str) -> SubjectFacts | None:
        return self._subjects.get(subject_id)

    def upsert_subject(self, subject: SubjectFacts) -> None:
        self._subjects[subject.subject_id] = subject

    def persona(self, *, subject_id: str | None) -> PersonaAssignment | None:
        del subject_id
        return self._personas[0] if self._personas else None

    def upsert_persona(self, persona: PersonaAssignment) -> None:
        self._personas = (persona,)

    def save_profile(self, profile: RuntimeProfile) -> None:
        self._profiles[profile.session_id] = profile
        self._profile_history[profile.runtime_profile_id] = profile
        session_issued = [
            profile_id
            for profile_id, issued in self._profile_history.items()
            if issued.session_id == profile.session_id
        ]
        # Bound per-session history so superseded profiles stay detectable as
        # stale while the authority cannot grow without limit.
        overflow = len(session_issued) - _MAX_PROFILE_HISTORY_PER_SESSION
        if overflow > 0:
            for profile_id in session_issued[:overflow]:
                del self._profile_history[profile_id]

    def current_profile(self, session_id: str) -> RuntimeProfile | None:
        return self._profiles.get(session_id)

    def profile_by_id(self, runtime_profile_id: str) -> RuntimeProfile | None:
        return self._profile_history.get(runtime_profile_id)


class RuntimeProfileRejected(PermissionError):
    """A missing, expired, stale, or tampered Runtime Profile failed closed."""


class RuntimeProfileService:
    """Deep session interface: resolve, authorize, sign, and fence in one call."""

    def __init__(
        self,
        *,
        authority: InMemoryRuntimeAuthority,
        policy: PolicyEngine,
        signing_key: bytes,
        profile_ttl: timedelta = timedelta(minutes=5),
        profile_id_factory: Callable[[], str] | None = None,
        subject_resolver: SubjectResolver | None = None,
        mode_resolver: ServiceModeResolver | None = None,
    ) -> None:
        if len(signing_key) < 16:
            raise ValueError("signing_key must contain at least 16 bytes")
        if profile_ttl <= timedelta(0):
            raise ValueError("profile_ttl must be positive")
        self.authority = authority
        self.policy = policy
        self.signing_key = signing_key
        self.profile_ttl = profile_ttl
        self._profile_id_factory = profile_id_factory or (lambda: str(uuid.uuid4()))
        self.subject_resolver = subject_resolver or SubjectResolver()
        self.mode_resolver = mode_resolver or ServiceModeResolver()

    def start(self, command: StartSessionCommand) -> RuntimeProfile:
        return self._issue(command, session_epoch=1)

    def switch_subject(self, command: SwitchSubjectCommand) -> RuntimeProfile:
        previous = self.authority.current_profile(command.session_id)
        if previous is None:
            raise LookupError("active runtime profile is unavailable")
        return self._issue(
            StartSessionCommand(
                session_id=command.session_id,
                device_id=previous.device_id,
                actor_id=command.actor_id,
                candidates=command.candidates,
                requested_capabilities=command.requested_capabilities,
                now=command.now,
                relationship_roles=command.relationship_roles,
                consent_kinds=command.consent_kinds,
                app_claimed_subject_id=command.app_claimed_subject_id,
                multiple_speakers=command.multiple_speakers,
                offline=command.offline,
            ),
            session_epoch=previous.session_epoch + 1,
        )

    def _issue(
        self,
        command: StartSessionCommand,
        *,
        session_epoch: int,
    ) -> RuntimeProfile:
        binding = self.authority.active_binding(command.device_id)
        if binding is None:
            raise LookupError("active binding is unavailable")
        resolution = self.subject_resolver.resolve(
            ResolveSubjectCommand(
                device_id=command.device_id,
                candidates=command.candidates,
                app_claimed_subject_id=command.app_claimed_subject_id,
                multiple_speakers=command.multiple_speakers,
                offline=command.offline,
            ),
            binding=binding,
        )
        subject = (
            self.authority.subject(resolution.active_subject_id)
            if resolution.active_subject_id is not None
            else None
        )
        category: SubjectCategoryValue = subject.category if subject else "unknown"
        age_band: AgeBandValue = subject.age_band if subject else "unknown"
        subject_revision = subject.revision if subject else 0
        service_mode = self.mode_resolver.resolve(
            declared_mode=binding.declared_mode,
            resolution=resolution,
            subject_category=category,
            age_band=age_band,
        )
        persona = self.authority.persona(subject_id=resolution.active_subject_id)
        if persona is None:
            raise LookupError("persona assignment is unavailable")
        profile_id = self._profile_id_factory()
        capabilities: list[Capability] = []
        obligations: list[PolicyObligationSpec] = []
        receipt_ids: list[str] = []
        for capability in command.requested_capabilities:
            purpose = canonical_profile_issue_purpose(capability)
            if capability in PROFILE_ISSUE_DEFERRED_CAPABILITIES:
                continue
            if capability not in PROFILE_ISSUE_SESSION_CAPABILITIES:
                raise ValueError(
                    f"capability is not classified for profile issue: {capability}"
                )
            decision = self.policy.decide(
                PolicyContext(
                    actor_id=command.actor_id,
                    subject_id=resolution.active_subject_id,
                    resource_owner_id=resolution.active_subject_id,
                    device_id=command.device_id,
                    capability=capability,
                    purpose=purpose,
                    declared_device_mode=binding.declared_mode,
                    current_session_mode=service_mode,
                    relationship_roles=command.relationship_roles,
                    subject_category=category,
                    age_band=age_band,
                    speaker_state=resolution.speaker_state,
                    speaker_confidence=resolution.speaker_confidence,
                    consent_kinds=command.consent_kinds,
                    device_trust="trusted" if not command.offline else "offline",
                    safety_state="normal",
                    jurisdiction="CN",
                    data_classification="ephemeral",
                    binding_id=binding.binding_id,
                    binding_version=binding.binding_version,
                    session_id=command.session_id,
                    session_epoch=session_epoch,
                    runtime_profile_id=profile_id,
                    subject_revision=subject_revision,
                    evaluated_at=command.now,
                )
            )
            if decision.effect == "deny":
                continue
            capabilities.append(capability)
            receipt_ids.append(decision.receipt_id)
            for obligation in decision.obligations:
                if obligation not in obligations:
                    obligations.append(obligation)
        unsigned = RuntimeProfile(
            runtime_profile_id=profile_id,
            device_id=command.device_id,
            session_id=command.session_id,
            actor_id=command.actor_id,
            binding_id=binding.binding_id,
            binding_version=binding.binding_version,
            active_subject_id=resolution.active_subject_id,
            subject_revision=subject_revision,
            subject_category=category,
            age_band=age_band,
            speaker_state=resolution.speaker_state,
            speaker_confidence=resolution.speaker_confidence,
            service_mode=service_mode,
            persona_assignment_id=persona.assignment_id,
            persona_id=persona.persona_id,
            persona_version=persona.persona_version,
            relationship_stage=persona.relationship_stage,
            policy_bundle_version=self.policy.policy_version,
            capabilities=tuple(capabilities),
            obligations=tuple(obligations),
            policy_receipt_ids=tuple(receipt_ids),
            session_epoch=session_epoch,
            issued_at=command.now,
            expires_at=command.now + self.profile_ttl,
            signature="",
        )
        profile = replace(unsigned, signature=self._signature(unsigned))
        self.authority.save_profile(profile)
        return profile

    def verify(self, profile: RuntimeProfile, *, now: datetime) -> bool:
        return self._invalid_reason(profile, now=now) is None

    def require_valid(self, profile: RuntimeProfile, *, now: datetime) -> None:
        """Raise ``RuntimeProfileRejected`` unless the profile is usable now."""
        invalid_reason = self._invalid_reason(profile, now=now)
        if invalid_reason is not None:
            raise RuntimeProfileRejected(invalid_reason)

    def authorize(
        self,
        profile: RuntimeProfile,
        *,
        capability: Capability,
        now: datetime,
        relationship_roles: frozenset[str] = frozenset(),
        consent_kinds: frozenset[str] = frozenset(),
        safety_state: str = "normal",
        data_classification: str,
    ) -> PolicyDecision:
        invalid_reason = self._invalid_reason(profile, now=now)
        if invalid_reason is not None:
            raise RuntimeProfileRejected(invalid_reason)
        if capability not in profile.capabilities:
            raise RuntimeProfileRejected("capability_not_in_runtime_profile")
        binding = self.authority.active_binding(profile.device_id)
        if (
            binding is None
            or binding.binding_id != profile.binding_id
            or binding.binding_version != profile.binding_version
        ):
            raise RuntimeProfileRejected("binding_version_stale")
        subject = (
            self.authority.subject(profile.active_subject_id)
            if profile.active_subject_id is not None
            else None
        )
        if subject is not None and subject.revision != profile.subject_revision:
            raise RuntimeProfileRejected("subject_revision_stale")
        return self.policy.decide(
            self._authorization_context(
                profile,
                capability=capability,
                now=now,
                relationship_roles=relationship_roles,
                consent_kinds=consent_kinds,
                safety_state=safety_state,
                data_classification=data_classification,
                binding=binding,
                subject=subject,
            )
        )

    def decide(
        self,
        profile: RuntimeProfile,
        *,
        capability: Capability,
        now: datetime,
        relationship_roles: frozenset[str] = frozenset(),
        consent_kinds: frozenset[str] = frozenset(),
        safety_state: str = "normal",
        data_classification: str,
    ) -> PolicyDecision:
        """Return an auditable allow/deny decision without enlarging the profile."""
        invalid_reason = self._invalid_reason(profile, now=now)
        if invalid_reason is not None:
            raise RuntimeProfileRejected(invalid_reason)
        binding = self.authority.active_binding(profile.device_id)
        if (
            binding is None
            or binding.binding_id != profile.binding_id
            or binding.binding_version != profile.binding_version
        ):
            raise RuntimeProfileRejected("binding_version_stale")
        subject = (
            self.authority.subject(profile.active_subject_id)
            if profile.active_subject_id is not None
            else None
        )
        if subject is not None and subject.revision != profile.subject_revision:
            raise RuntimeProfileRejected("subject_revision_stale")
        context = self._authorization_context(
            profile,
            capability=capability,
            now=now,
            relationship_roles=relationship_roles,
            consent_kinds=consent_kinds,
            safety_state=safety_state,
            data_classification=data_classification,
            binding=binding,
            subject=subject,
        )
        if capability not in profile.capabilities:
            return self.policy.deny(
                context,
                reason_code="capability_not_in_runtime_profile",
            )
        return self.policy.decide(context)

    @staticmethod
    def _authorization_context(
        profile: RuntimeProfile,
        *,
        capability: Capability,
        now: datetime,
        relationship_roles: frozenset[str],
        consent_kinds: frozenset[str],
        safety_state: str,
        data_classification: str,
        binding: BindingSnapshot,
        subject: SubjectFacts | None,
    ) -> PolicyContext:
        return PolicyContext(
            actor_id=profile.actor_id,
            subject_id=profile.active_subject_id,
            resource_owner_id=profile.active_subject_id,
            device_id=profile.device_id,
            capability=capability,
            purpose=canonical_runtime_decision_purpose(capability),
            declared_device_mode=binding.declared_mode,
            current_session_mode=profile.service_mode,
            relationship_roles=relationship_roles,
            subject_category=subject.category if subject else "unknown",
            age_band=subject.age_band if subject else "unknown",
            speaker_state=profile.speaker_state,
            speaker_confidence=profile.speaker_confidence,
            consent_kinds=consent_kinds,
            device_trust="trusted",
            safety_state=safety_state,
            jurisdiction="CN",
            data_classification=data_classification,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            session_id=profile.session_id,
            session_epoch=profile.session_epoch,
            runtime_profile_id=profile.runtime_profile_id,
            subject_revision=profile.subject_revision,
            evaluated_at=now,
        )

    def _invalid_reason(self, profile: RuntimeProfile, *, now: datetime) -> str | None:
        if now >= profile.expires_at:
            return "runtime_profile_expired"
        if now < profile.issued_at:
            return "runtime_profile_not_yet_valid"
        if not hmac.compare_digest(profile.signature, self._signature(profile)):
            return "runtime_profile_signature_invalid"
        if self.authority.current_profile(profile.session_id) != profile:
            return "runtime_profile_stale"
        return None

    def _signature(self, profile: RuntimeProfile) -> str:
        return sign_runtime_profile_payload(
            runtime_profile_wire_payload(profile),
            signing_key=self.signing_key,
        )
