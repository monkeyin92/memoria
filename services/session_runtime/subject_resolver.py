"""Resolve the active natural-person subject without account-owner fallback."""

from __future__ import annotations

from dataclasses import dataclass

from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceDeclaredModeValue,
    ServiceModeValue,
    SpeakerStateValue,
)

#: The device's own authenticated media session names the binding's primary
#: subject: a device serves the one person it is bound to, so that person is
#: the one talking on it. Never produced for an app or voice claim.
DEVICE_BINDING_PRIMARY_REASON = "device_binding_primary"


#: The app/control path confirms the one person a one-to-one binding serves.
#: The app user is authenticated, but nobody's voice or presence is verified:
#: this only records that the binding names exactly one subject.
SOLE_BOUND_SUBJECT_REASON = "sole_bound_subject"


def sole_bound_subject_id(binding: BindingSnapshot) -> str | None:
    """The binding's only primary subject, or ``None`` when it is not one-to-one.

    ``family_shared`` bindings serve several people and never qualify, nor
    does any binding with zero or several primary subjects.
    """
    if binding.declared_mode == "family_shared":
        return None
    if len(binding.primary_subject_ids) != 1:
        return None
    return binding.primary_subject_ids[0]


@dataclass(frozen=True, slots=True)
class SubjectCandidate:
    subject_id: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.subject_id.strip():
            raise ValueError("subject_id must not be empty")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class BindingSnapshot:
    binding_id: str
    device_id: str
    binding_version: int
    declared_mode: DeviceDeclaredModeValue
    primary_subject_ids: tuple[str, ...]
    member_subject_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolveSubjectCommand:
    device_id: str
    candidates: tuple[SubjectCandidate, ...] = ()
    app_claimed_subject_id: str | None = None
    # The control path asks to confirm the binding's sole subject; ignored
    # unless the binding is one-to-one (see ``sole_bound_subject_id``).
    sole_bound_subject: bool = False
    device_bound_subject_id: str | None = None
    multiple_speakers: bool = False
    offline: bool = False


@dataclass(frozen=True, slots=True)
class SubjectResolution:
    active_subject_id: str | None
    speaker_state: SpeakerStateValue
    speaker_confidence: float | None
    service_mode: ServiceModeValue
    reason_code: str
    requires_confirmation: bool


class SubjectResolver:
    """Resolve a binding member or return an explicit unknown-safe result."""

    def __init__(
        self,
        *,
        confirm_threshold: float = 0.85,
        ambiguity_margin: float = 0.10,
    ) -> None:
        if not 0 < confirm_threshold <= 1:
            raise ValueError("confirm_threshold must be in (0, 1]")
        if not 0 < ambiguity_margin <= 1:
            raise ValueError("ambiguity_margin must be in (0, 1]")
        self.confirm_threshold = confirm_threshold
        self.ambiguity_margin = ambiguity_margin

    def resolve(
        self,
        command: ResolveSubjectCommand,
        *,
        binding: BindingSnapshot,
    ) -> SubjectResolution:
        if command.device_id != binding.device_id:
            return self._unknown("binding_device_mismatch")
        if command.offline:
            return self._unknown("policy_snapshot_unavailable")
        if command.multiple_speakers:
            return self._unknown("multiple_speakers")
        if command.device_bound_subject_id is not None:
            if command.device_bound_subject_id not in binding.primary_subject_ids:
                return self._unknown("device_subject_not_primary")
            return SubjectResolution(
                active_subject_id=command.device_bound_subject_id,
                speaker_state="confirmed",
                speaker_confidence=None,
                service_mode="family_shared",
                reason_code=DEVICE_BINDING_PRIMARY_REASON,
                requires_confirmation=False,
            )
        if command.app_claimed_subject_id is not None:
            if command.app_claimed_subject_id not in binding.member_subject_ids:
                return self._unknown("app_subject_not_bound")
            return SubjectResolution(
                active_subject_id=command.app_claimed_subject_id,
                speaker_state="confirmed",
                speaker_confidence=None,
                service_mode="family_shared",
                reason_code="app_subject_confirmed",
                requires_confirmation=False,
            )
        if command.sole_bound_subject:
            sole = sole_bound_subject_id(binding)
            if sole is None:
                return self._unknown("binding_not_one_to_one")
            return SubjectResolution(
                active_subject_id=sole,
                speaker_state="confirmed",
                speaker_confidence=None,
                service_mode="family_shared",
                reason_code=SOLE_BOUND_SUBJECT_REASON,
                requires_confirmation=False,
            )
        candidates = sorted(command.candidates, key=lambda item: item.confidence, reverse=True)
        if not candidates or candidates[0].confidence < self.confirm_threshold:
            return self._unknown("speaker_confidence_low")
        if (
            len(candidates) > 1
            and candidates[0].confidence - candidates[1].confidence
            < self.ambiguity_margin
        ):
            return self._unknown("speaker_candidates_ambiguous")
        candidate = candidates[0]
        if candidate.subject_id not in binding.member_subject_ids:
            return self._unknown("speaker_not_bound")
        return SubjectResolution(
            active_subject_id=candidate.subject_id,
            speaker_state="confirmed",
            speaker_confidence=candidate.confidence,
            service_mode="family_shared",
            reason_code="speaker_confirmed",
            requires_confirmation=False,
        )

    @staticmethod
    def _unknown(reason_code: str) -> SubjectResolution:
        return SubjectResolution(
            active_subject_id=None,
            speaker_state="unconfirmed",
            speaker_confidence=None,
            service_mode="unknown_safe",
            reason_code=reason_code,
            requires_confirmation=True,
        )
