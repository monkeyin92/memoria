"""Capability-specific receipt verification adapter port (audit 3).

The only authority is ``ReceiptVerifierPort``: its implementation validates a
server-issued ``PolicyReceiptV2`` (via the Control wire/signature envelope or
receipt store lookup) and derives an unforgeable ``VerifiedReceiptEvidence``
through :meth:`VerifiedReceiptEvidence.from_policy_receipt`, which pins the
receipt to one exact profile (actor/subject/device/binding/version/profile/
epoch/subject revision).  There is no custom wire format here: the purpose
values come from the Policy V2 authority (``services.policy.context``
``PURPOSE_VALUES``) through the explicit capability->purpose contract below.

Production wiring: until the Control receipt authority is wired, the
``DefaultDenyReceiptVerifier`` is installed and every sensitive persistence
decision stays closed.  This is an accepted fail-closed core: persistence is
NOT end-to-end functional yet (audit 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from services.agent.src.runtime_profile import RuntimeProfile
from services.policy.context import PURPOSE_VALUES
from services.policy.receipts import PolicyReceiptV2

_RECEIPT_TOKEN = object()

# Versioned capability -> purpose contract (audit 4), aligned with the Policy
# V2 authority (PURPOSE_VALUES).  Purpose is an independent PolicyContext
# field; these are the agreed values for the sensitive side effects.
RECEIPT_PURPOSE_CONTRACT: Final[dict[str, str]] = {
    "chat": "user_request",
    "memory_capture": "memory_capture",
    "raw_audio_retention": "raw_audio",
    "model_training_contribution": "model_training",
}


@dataclass(frozen=True, slots=True, init=False)
class VerifiedReceiptEvidence:
    """A verified capability receipt bound to one exact profile."""

    receipt_id: str
    capability: str
    purpose: str
    actor_id: str
    subject_id: str
    device_id: str
    binding_id: str
    binding_version: int
    runtime_profile_id: str
    session_id: str
    session_epoch: int
    expires_at: datetime

    def __init__(
        self,
        *,
        receipt_id: str,
        capability: str,
        purpose: str,
        actor_id: str,
        subject_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        runtime_profile_id: str,
        session_id: str,
        session_epoch: int,
        expires_at: datetime,
        _token: object,
    ) -> None:
        if _token is not _RECEIPT_TOKEN:
            raise TypeError("VerifiedReceiptEvidence: forged construction rejected")
        if purpose not in PURPOSE_VALUES:
            raise ValueError(f"purpose {purpose!r} is not a Policy V2 purpose")
        if purpose != RECEIPT_PURPOSE_CONTRACT.get(capability):
            raise ValueError(
                f"receipt purpose {purpose!r} does not match the capability "
                f"contract for {capability!r}"
            )
        for name, value in (
            ("receipt_id", receipt_id),
            ("capability", capability),
            ("actor_id", actor_id),
            ("subject_id", subject_id),
            ("device_id", device_id),
            ("binding_id", binding_id),
            ("runtime_profile_id", runtime_profile_id),
            ("session_id", session_id),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a bounded non-blank string")
        if (
            not isinstance(binding_version, int)
            or isinstance(binding_version, bool)
            or binding_version < 1
        ):
            raise ValueError("binding_version must be a positive integer")
        if (
            not isinstance(session_epoch, int)
            or isinstance(session_epoch, bool)
            or session_epoch < 1
        ):
            raise ValueError("session_epoch must be a positive integer")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must carry a timezone")
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "binding_id", binding_id)
        object.__setattr__(self, "binding_version", binding_version)
        object.__setattr__(self, "runtime_profile_id", runtime_profile_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "session_epoch", session_epoch)
        object.__setattr__(self, "expires_at", expires_at)

    def matches_profile(self, profile: RuntimeProfile) -> bool:
        """Exact per-fence binding: actor/subject/device/binding/version/
        profile/epoch must all match (audit 3)."""

        return (
            self.actor_id == profile.actor_id
            and self.subject_id == profile.active_subject_id
            and self.device_id == profile.device_id
            and self.binding_id == profile.binding_id
            and self.binding_version == profile.binding_version
            and self.runtime_profile_id == profile.runtime_profile_id
            and self.session_id == profile.session_id
            and self.session_epoch == profile.session_epoch
        )

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC) >= self.expires_at.astimezone(UTC)


def evidence_from_policy_receipt(
    receipt: PolicyReceiptV2,
    *,
    capability: str,
    profile: RuntimeProfile,
) -> VerifiedReceiptEvidence:
    """Verifier-internal conversion seam from a Policy V2 receipt.

    NOT an authority by itself: its safety comes from the ``ReceiptVerifierPort``
    implementation that validates the server-issued receipt (signature /
    authoritative receipt store) before calling this, and from the gate
    injection boundary.  Rejects ``deny`` receipts, receipts whose obligations
    forbid the side effect, contract-deviating purpose/capability, and any
    actor/subject/device/binding/version/profile/epoch/subject-revision
    mismatch against the signed RuntimeProfile.  ``exact_fence`` is not
    re-checked here: it records whether the PolicyEngine decision referenced
    authoritative consent/relationship evidence, and its enforcement belongs
    to the ``ReceiptVerifierPort`` authority (with the full PolicyContext)
    before this conversion seam runs.
    """

    if receipt.effect not in {"allow", "allow_with_obligations"}:
        raise ValueError("receipt effect must be allow or allow_with_obligations")
    obligation_codes = {obligation.code for obligation in receipt.obligations}
    if "DO_NOT_PERSIST" in obligation_codes:
        raise ValueError("receipt obligations forbid persistence")
    if "RETENTION_TTL" in obligation_codes:
        params = next(
            obligation.params
            for obligation in receipt.obligations
            if obligation.code == "RETENTION_TTL"
        )
        if params.retention_ttl_seconds is None or params.retention_ttl_seconds <= 0:
            raise ValueError("RETENTION_TTL requires a positive retention window")
    if receipt.capability != capability:
        raise ValueError("receipt capability does not match the requested one")
    if (
        receipt.actor_id != profile.actor_id
        or receipt.subject_id != profile.active_subject_id
        or receipt.device_id != profile.device_id
        or receipt.binding_id != profile.binding_id
        or receipt.binding_version != profile.binding_version
        or receipt.runtime_profile_id != profile.runtime_profile_id
        or receipt.session_id != profile.session_id
        or receipt.session_epoch != profile.session_epoch
        or receipt.subject_revision != profile.subject_revision
    ):
        raise ValueError("receipt fence does not match the signed profile")
    if receipt.expires_at.tzinfo is None:
        raise ValueError("receipt expires_at must carry a timezone")
    return VerifiedReceiptEvidence(
        receipt_id=receipt.receipt_id,
        capability=capability,
        purpose=receipt.purpose,
        actor_id=receipt.actor_id,
        subject_id=receipt.subject_id or "",
        device_id=receipt.device_id,
        binding_id=receipt.binding_id,
        binding_version=receipt.binding_version,
        runtime_profile_id=receipt.runtime_profile_id,
        session_id=receipt.session_id,
        session_epoch=receipt.session_epoch,
        expires_at=receipt.expires_at,
        _token=_RECEIPT_TOKEN,
    )


class ReceiptVerifierPort(Protocol):
    """Validates one server-issued Policy V2 receipt for a capability."""

    def verify_receipt(
        self,
        *,
        capability: str,
        profile: RuntimeProfile,
    ) -> VerifiedReceiptEvidence | None:
        """Return verified evidence or None (fail closed)."""


class DefaultDenyReceiptVerifier:
    """Fail-closed core: no receipt can be proven until the Control receipt
    authority is wired (audit 3)."""

    def verify_receipt(
        self,
        *,
        capability: str,
        profile: RuntimeProfile,
    ) -> VerifiedReceiptEvidence | None:
        del capability, profile
        return None


__all__ = [
    "DefaultDenyReceiptVerifier",
    "RECEIPT_PURPOSE_CONTRACT",
    "ReceiptVerifierPort",
    "VerifiedReceiptEvidence",
    "evidence_from_policy_receipt",
]
