"""Per-session RuntimeProfile gate: fence-epoch binding and fail-closed apply.

Owned by the Orchestrator (which owns fences, the fence gate and the working
context), so DuplexRuntime stays a thin client.  The gate:

- accepts only ``VerifiedRuntimeProfile`` (produced by the HMAC verifier
  factory) or a signed wire mapping — a bare ``RuntimeProfile`` dataclass is
  never accepted as a proof (P0-1);
- binds the profile to the exact expected session/device and only lets
  same-epoch replays through when the identity+permission fingerprint is
  unchanged; any identity/binding/persona/permission change requires a higher
  session epoch (P0-2);
- advances the identity epoch on subject switch and clears the private working
  context (PR-08); pre-switch fences are void;
- revokes an epoch once its profile expires or is replaced by an invalid
  payload, so sensitive side effects (tools, TTS, memory/history) stop even
  when the fence itself is unchanged (P0-5);
- records the bounded trust metrics from remediation doc 14.2
  (``subject_resolution_unknown_rate``, ``runtime_profile_expired_use_attempts``),
  counting ``confirmed`` only for a confirmed speaker in a non-safe mode (P1).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.obligation_executor import (
    PersistenceDecision,
    decide_persistence,
)
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.receipt_evidence import (
    DefaultDenyReceiptVerifier,
    ReceiptVerifierPort,
    VerifiedReceiptEvidence,
)
from services.agent.src.runtime_profile import (
    VERIFY_KEY_ENV,
    RuntimeProfile,
    VerifiedRuntimeProfile,
    is_expired_profile_payload,
    parse_runtime_profile,
)


@dataclass
class RuntimeProfileGate:
    """Binds at most one signed RuntimeProfile to the current identity epoch."""

    metrics: MetricsRegistry
    bump_epoch: Callable[[int], GenerationFence]
    expected_session_id: str
    expected_device_id: str | None = None
    expected_actor_id: str | None = None
    expected_binding_id: str | None = None
    expected_binding_version: int | None = None
    expected_active_subject_id: str | None = None
    expected_subject_revision: int | None = None
    # Device-visible monotonic config/profile projection. It is compared with
    # the Control policy envelope, never with signed identity session_epoch.
    expected_device_profile_version: int | None = None
    verify_key: str | None = field(
        default_factory=lambda: os.getenv(VERIFY_KEY_ENV) or None
    )
    clock: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC),
        repr=False,
    )
    receipt_verifier: ReceiptVerifierPort = field(
        default_factory=DefaultDenyReceiptVerifier,
        repr=False,
    )
    _profile: VerifiedRuntimeProfile | None = field(default=None, init=False, repr=False)
    _revoked_epochs: set[int] = field(default_factory=set, init=False, repr=False)
    _last_fingerprint: tuple[object, ...] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.expected_session_id.strip():
            raise ValueError("expected_session_id must not be blank")

    @property
    def current(self) -> VerifiedRuntimeProfile | None:
        return self._profile

    def apply(
        self,
        payload: object,
        current_fence: GenerationFence,
        *,
        now: datetime | None = None,
    ) -> VerifiedRuntimeProfile | None:
        """Freeze one server-signed profile, or fail closed to ``None``.

        Only ``VerifiedRuntimeProfile`` (verifier-factory output) or a signed
        wire mapping is accepted; a bare ``RuntimeProfile`` dataclass is never
        a proof and fails closed (P0-1).  A higher ``session_epoch`` advances
        the identity fence (and clears the private working context via the
        orchestrator); a stale lower-epoch profile is ignored so a late profile
        can never re-grant identity; a same-epoch profile whose identity or
        permission fingerprint differs is rejected without replacement (P0-2).
        """

        now = now or self.clock()
        if isinstance(payload, VerifiedRuntimeProfile):
            verified: VerifiedRuntimeProfile | None = payload
        elif isinstance(payload, Mapping):
            if is_expired_profile_payload(payload, now=now):
                self.metrics.inc_runtime_profile_expired_use_attempt("expired_at_parse")
            verified = parse_runtime_profile(payload, now=now, verify_key=self.verify_key)
        else:
            # Bare RuntimeProfile or anything else: not a proof, fail closed.
            verified = None
        if verified is None:
            self._revoke_current()
            self.metrics.inc_subject_resolution("unknown")
            return None
        profile = verified.profile
        if profile.session_id != self.expected_session_id or (
            self.expected_device_id is not None
            and profile.device_id != self.expected_device_id
        ):
            # A validly signed profile for another session/device must never
            # be replayed into this one (P0-2).
            self._revoke_current()
            self.metrics.inc_subject_resolution("unknown")
            return None
        if (
            (self.expected_actor_id is not None and profile.actor_id != self.expected_actor_id)
            or (
                self.expected_binding_id is not None
                and profile.binding_id != self.expected_binding_id
            )
            or (
                self.expected_binding_version is not None
                and profile.binding_version != self.expected_binding_version
            )
            or (
                self.expected_active_subject_id is not None
                and profile.active_subject_id != self.expected_active_subject_id
            )
            or (
                self.expected_subject_revision is not None
                and profile.subject_revision != self.expected_subject_revision
            )
        ):
            # A valid signature is not sufficient when the profile belongs to
            # another binding/subject fence.  Do not let a late or replayed
            # profile cross the caller-provided identity context.
            self._revoke_current()
            self.metrics.inc_subject_resolution("unknown")
            return None
        if profile.session_epoch < current_fence.session_epoch:
            return None
        fingerprint = profile.identity_fingerprint()
        if profile.session_epoch == current_fence.session_epoch:
            if self._last_fingerprint is not None and self._last_fingerprint == fingerprint:
                # Idempotent replay/refresh of the same identity/permission
                # surface (also restores a revoked epoch): keep the epoch.
                self._profile = verified
                self._revoked_epochs.discard(profile.session_epoch)
                self._record_resolution(profile)
                return verified
            if self._profile is not None or profile.session_epoch in self._revoked_epochs:
                # Same epoch, different identity/permission: must fail closed
                # and never replace the current subject (P0-2).
                self._revoke_current()
                self.metrics.inc_subject_resolution("unknown")
                return None
            # First profile of this epoch (e.g. session start at epoch 1).
            self._profile = verified
            self._last_fingerprint = fingerprint
            self._revoked_epochs.discard(profile.session_epoch)
            self._record_resolution(profile)
            return verified
        # Higher epoch: advance the identity fence and clear private context.
        self.bump_epoch(profile.session_epoch)
        self._profile = verified
        self._last_fingerprint = fingerprint
        self._revoked_epochs.discard(profile.session_epoch)
        self._record_resolution(profile)
        return verified

    def for_fence(
        self,
        fence: GenerationFence,
        *,
        current_fence: GenerationFence,
    ) -> VerifiedRuntimeProfile | None:
        """Resolve the profile for one generation, or ``None`` (unknown_safe).

        Pre-switch fences are always void: once the identity epoch advances,
        no old generation may read the subject it was frozen under (PR-08).
        """

        if fence.session_epoch != current_fence.session_epoch:
            return None
        self._expire_if_needed()
        profile = self._profile
        if profile is None or profile.profile.session_epoch != fence.session_epoch:
            return None
        return profile

    def output_allowed(
        self,
        fence: GenerationFence,
        *,
        current_fence: GenerationFence,
    ) -> bool:
        """Gate LLM/TTS/tool sinks against profile revocation (P0-5).

        A fence whose epoch never held a profile stays allowed (unknown-safe
        conversation); once an epoch has held a profile that expired or was
        invalidated, its outputs are refused even though the fence is
        unchanged.
        """

        if fence.session_epoch != current_fence.session_epoch:
            return False
        self._expire_if_needed()
        return fence.session_epoch not in self._revoked_epochs

    def degrade(self, current_fence: GenerationFence) -> GenerationFence:
        """Authority-loss transition: void the current identity epoch and
        advance to a fresh degraded epoch with no profile (true unknown-safe).

        The old epoch stays revoked, so its late LLM/TTS/tool/archive/UI
        results are refused by the epoch mismatch in every lookup.  The new
        degraded epoch never held a profile, so ordinary unknown-safe chat can
        continue.  Idempotent: an epoch without a profile that was never
        revoked is already degraded and is not bumped again.
        """

        if (
            self._profile is None
            and current_fence.session_epoch not in self._revoked_epochs
        ):
            return current_fence
        if self._profile is not None:
            self._revoked_epochs.add(self._profile.profile.session_epoch)
        self._profile = None
        self._last_fingerprint = None
        return self.bump_epoch(current_fence.session_epoch + 1)

    def sensitive_effect_allowed(
        self,
        fence: GenerationFence,
        *,
        current_fence: GenerationFence,
        capability: str | None = None,
    ) -> bool:
        """Gate sensitive side effects by profile (alias of ``permits``)."""

        return self.permits(fence, current_fence=current_fence, capability=capability)

    def permits(
        self,
        fence: GenerationFence,
        *,
        current_fence: GenerationFence,
        capability: str | None = None,
    ) -> bool:
        """Full production-side permission check (P0-5).

        A sensitive side effect (tools, memory/history write, learning,
        projection, prefetch) is only allowed when the fence carries the
        current session epoch AND a valid, unexpired signed RuntimeProfile
        with a confirmed speaker AND (when given) the canonical capability.
        Without a profile the effect is refused — unknown-safe chat never
        borrows ModePolicy-granted sensitivity.
        """

        if fence.session_epoch != current_fence.session_epoch:
            return False
        self._expire_if_needed()
        if fence.session_epoch in self._revoked_epochs:
            return False
        if self.expected_device_id is None:
            # Without a bound device the signed profile cannot be tied to this
            # session's device: sensitive effects stay fully denied (P1-9).
            return False
        profile = self._profile
        if profile is None:
            return False
        if profile.profile.speaker_state != "confirmed":
            return False
        if capability is not None and capability not in profile.profile.capabilities:
            return False
        return True

    def event_identity(
        self,
        fence: GenerationFence,
        *,
        current_fence: GenerationFence,
    ) -> dict[str, object]:
        """§11.5 event identity resolved from the event's own fence."""

        profile = self.for_fence(fence, current_fence=current_fence)
        return {
            "session_epoch": fence.session_epoch,
            "device_id": profile.profile.device_id if profile is not None else None,
            "subject_revision": (
                profile.profile.subject_revision if profile is not None else None
            ),
            "active_subject_id": (
                profile.profile.active_subject_id if profile is not None else None
            ),
            "runtime_profile_id": (
                profile.profile.runtime_profile_id if profile is not None else None
            ),
            "actor_id": profile.profile.actor_id if profile is not None else None,
            "binding_id": profile.profile.binding_id if profile is not None else None,
            "binding_version": (
                profile.profile.binding_version if profile is not None else None
            ),
        }

    def persistence_decision(
        self,
        fence: GenerationFence,
        *,
        current_fence: GenerationFence,
    ) -> PersistenceDecision:
        """Persistence decision for one event's own fence (P0-6).

        Sensitive persistence requires the matching capability-specific
        verified receipt evidence bound to this exact profile/epoch; the
        mapping is empty by default, so persistence stays closed (audit 3).
        """

        if fence.session_epoch != current_fence.session_epoch:
            return PersistenceDecision(allowed=False)
        self._expire_if_needed()
        if fence.session_epoch in self._revoked_epochs:
            return PersistenceDecision(allowed=False)
        if self._profile is None or self._profile.profile.session_epoch != fence.session_epoch:
            return PersistenceDecision(allowed=False)
        return decide_persistence(
            self._profile,
            memory_capture_evidence=self._verified_receipt_for(
                "memory_capture", fence, current_fence
            ),
            raw_audio_evidence=self._verified_receipt_for(
                "raw_audio_retention", fence, current_fence
            ),
            training_evidence=self._verified_receipt_for(
                "model_training_contribution", fence, current_fence
            ),
        )

    def _verified_receipt_for(
        self,
        capability: str,
        fence: GenerationFence,
        current_fence: GenerationFence,
    ) -> VerifiedReceiptEvidence | None:
        if fence.session_epoch != current_fence.session_epoch:
            return None
        self._expire_if_needed()
        profile = self._profile
        if profile is None:
            return None
        evidence = self.receipt_verifier.verify_receipt(
            capability=capability,
            profile=profile.profile,
        )
        if evidence is None:
            return None
        if evidence.receipt_id not in profile.profile.policy_receipt_ids:
            # The receipt must be listed in the signed RuntimeProfile's
            # policy_receipt_ids; anything else is not authorized (audit 1).
            return None
        if not evidence.matches_profile(profile.profile):
            return None
        if evidence.is_expired(self.clock()):
            return None
        return evidence

    def _record_resolution(self, profile: RuntimeProfile) -> None:
        """Count the bounded subject-resolution outcome (P1)."""

        status = (
            "confirmed"
            if profile.speaker_state == "confirmed"
            and profile.service_mode != "unknown_safe"
            else "unknown"
        )
        self.metrics.inc_subject_resolution(status)

    def _expire_if_needed(self) -> None:
        if self._profile is not None and self._profile.profile.is_expired(self.clock()):
            self.metrics.inc_runtime_profile_expired_use_attempt("expired_at_use")
            self._revoke_current()

    def _revoke_current(self) -> None:
        if self._profile is not None:
            self._revoked_epochs.add(self._profile.profile.session_epoch)
        self._profile = None


__all__ = ["RuntimeProfileGate"]
