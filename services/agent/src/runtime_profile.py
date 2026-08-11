"""Fail-closed RuntimeProfile parsing for the realtime agent.

The server signs one short-lived ``RuntimeProfile`` per session (remediation
doc 7.3 / PR-07): ``active_subject_id``, ``runtime_profile_id``,
``session_epoch``, canonical ``service_mode``, speaker state, expiry, the full
canonical binding/persona/policy fields and a small canonical
capability/obligation allowlist.  The agent never assembles rules itself: a
missing, malformed, unverifiable, expired or epoch-mismatched profile yields
``None``, and every consumer must then behave as ``unknown_safe``.  In
particular the account owner or a voice-speaker decision is never a substitute
for the profile's subject.

Signature contract (Control API ``runtime_profile_wire_payload`` /
``sign_runtime_profile_payload``): the wire payload IS the canonical signed
document.  HMAC-SHA256 over
``json.dumps(wire_payload_without_signature, sort_keys=True, separators=(",", ":"))``
with datetimes already serialised as ISO-8601 strings and the nested
``persona`` object included verbatim, plus ``signature_schema`` required to be
``runtime-profile-v1``.  The verifier rebuilds the canonical bytes from every
field it receives (nothing is verified over a subset), so any unsigned or
tampered field fails the check.  The verify key is injected via
``MEMORIA_RUNTIME_PROFILE_VERIFY_KEY``; production without a key fails closed
(every profile is rejected).

The verified result is returned as ``VerifiedRuntimeProfile``, which only this
module's factory can produce: a bare ``RuntimeProfile`` dataclass is a data
carrier, never a proof of server issuance (P0-1).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBand,
    Capability,
    PolicyObligation,
    RuntimeProfileSigned,
    RuntimeProfileSignedV2,
    ServiceMode,
)

VERIFY_KEY_ENV: Final[str] = "MEMORIA_RUNTIME_PROFILE_VERIFY_KEY"
SIGNATURE_SCHEMA: Final[str] = "runtime-profile-v1"
RUNTIME_PROFILE_V2_SIGNATURE_SCHEMA: Final[str] = "runtime-profile-v2"
SUPPORTED_SIGNATURE_SCHEMAS: Final[frozenset[str]] = frozenset(
    {SIGNATURE_SCHEMA, RUNTIME_PROFILE_V2_SIGNATURE_SCHEMA}
)

_VERIFIED_TOKEN = object()

# Canonical signed subject categories (contract §8.1): the wire only carries
# unknown / minor / adult.  "student" is a renderer-side derivation.
CanonicalSubjectCategory = Literal["minor", "adult", "unknown"]

# Unknown-safe mode only keeps the explicitly allowed conversation surface
# (remediation doc 4.3: chat and temporary English practice; tutor delivery
# and learning-progress persistence are NOT part of the unknown-safe
# surface); anything else is stripped or the profile is rejected.
UNKNOWN_SAFE_CAPABILITIES: Final[frozenset[str]] = frozenset({"chat", "english_practice"})
UNKNOWN_SAFE_REQUIRED_OBLIGATIONS: Final[frozenset[str]] = frozenset(
    {
        "DO_NOT_PERSIST",
        "NO_MODEL_TRAINING",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "REQUIRE_SPEAKER_CONFIRMATION",
    }
)

# Structurally adult-only capabilities: a minor subject must never carry them
# (remediation doc 4.2).  voice_profile_create / memory_recall_private /
# guardian_summary_view stay legal for minors (subject routing, consented
# memory, guardian summaries); payment / raw_audio_retention /
# model_training_contribution are default-denied and governed by the policy
# engine at the call site, never hard-pinned by the wire parser (audit 4).
MINOR_FORBIDDEN_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "device_ownership_transfer",
    }
)
# guardian_summary_view may name a minor active subject; whether the caller is
# an entitled guardian is enforced by Control relationship/policy gates at the
# call site, not by this structural parser.
ADULT_ONLY_SERVICE_MODES: Final[frozenset[str]] = frozenset(
    {"adult_companion", "senior_companion", "adult_archive", "self_preview", "legacy_access"}
)


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """The frozen, versioned subject/runtime contract consumed by the agent."""

    runtime_profile_id: str
    actor_id: str
    binding_id: str
    binding_version: int
    subject_revision: int
    active_subject_id: str | None
    subject_category: CanonicalSubjectCategory
    age_band: str
    speaker_state: Literal["unknown", "unconfirmed", "confirmed"]
    speaker_confidence: float | None
    service_mode: str
    persona_assignment_id: str
    persona_id: str
    persona_version: int
    relationship_stage: str
    policy_bundle_version: str
    policy_receipt_ids: tuple[str, ...]
    session_epoch: int
    issued_at: datetime
    expires_at: datetime
    session_id: str
    device_id: str
    capabilities: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "runtime_profile_id",
            "actor_id",
            "binding_id",
            "persona_assignment_id",
            "persona_id",
            "relationship_stage",
            "policy_bundle_version",
            "session_id",
            "device_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a non-blank string of at most 128 chars")
        for name in ("binding_version", "subject_revision", "persona_version", "session_epoch"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.binding_version < 1 or self.persona_version < 1:
            raise ValueError("binding_version and persona_version must be >= 1")
        if self.session_epoch < 1:
            raise ValueError("an issued runtime profile must carry session_epoch >= 1")
        if self.active_subject_id is not None and (
            not self.active_subject_id.strip() or len(self.active_subject_id) > 128
        ):
            raise ValueError(
                "active_subject_id must be a non-blank string of at most 128 chars or null"
            )
        if self.subject_category not in {"minor", "adult", "unknown"}:
            raise ValueError(f"unknown subject category: {self.subject_category!r}")
        if self.age_band not in AgeBand.values():
            raise ValueError(f"age_band is not canonical: {self.age_band!r}")
        if self.subject_category == "minor" and self.age_band not in {"under_14", "14_17"}:
            raise ValueError("minor subject category requires a minor age band")
        if self.subject_category == "adult" and self.age_band != "adult":
            raise ValueError("adult subject category requires the adult age band")
        if self.speaker_state not in {"unknown", "unconfirmed", "confirmed"}:
            raise ValueError(f"unknown speaker state: {self.speaker_state!r}")
        if self.speaker_confidence is not None and (
            isinstance(self.speaker_confidence, bool)
            or not 0.0 <= float(self.speaker_confidence) <= 1.0
        ):
            raise ValueError("speaker_confidence must be a number in [0, 1] or null")
        if self.service_mode not in ServiceMode.values():
            raise ValueError(f"service mode is not canonical: {self.service_mode!r}")
        if self.active_subject_id is None and not (
            self.speaker_state != "confirmed" and self.service_mode == "unknown_safe"
        ):
            # P0-3: a null subject is only legal for an unconfirmed speaker in
            # the fail-closed unknown_safe mode; confirmed speakers must name
            # the active subject.
            raise ValueError(
                "active_subject_id is required unless speaker is unconfirmed "
                "and service_mode is unknown_safe"
            )
        if (
            not isinstance(self.session_epoch, int)
            or isinstance(self.session_epoch, bool)
            or self.session_epoch < 0
        ):
            raise ValueError("session_epoch must be a non-negative integer")
        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"{name} must carry a timezone")
        if self.issued_at >= self.expires_at:
            raise ValueError("issued_at must be before expires_at")
        for capability in self.capabilities:
            if capability not in Capability.values():
                raise ValueError(f"capability is not canonical: {capability!r}")
        for obligation in self.obligations:
            if obligation not in PolicyObligation.values():
                raise ValueError(f"obligation is not canonical: {obligation!r}")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 128
            for item in self.policy_receipt_ids
        ):
            raise ValueError("policy_receipt_ids must be a list of bounded strings")

    def identity_fingerprint(self) -> tuple[object, ...]:
        """Identity+permission fingerprint for same-epoch replay checks.

        Any change to subject, binding, persona assignment or the permission
        surface requires a higher session epoch (P0-2).
        """

        return (
            self.runtime_profile_id,
            self.actor_id,
            self.binding_id,
            self.binding_version,
            self.subject_revision,
            self.active_subject_id,
            self.subject_category,
            self.age_band,
            self.speaker_state,
            self.speaker_confidence,
            self.persona_assignment_id,
            self.persona_id,
            self.persona_version,
            self.relationship_stage,
            self.policy_bundle_version,
            self.policy_receipt_ids,
            self.service_mode,
            self.capabilities,
            self.obligations,
        )

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC) >= self.expires_at.astimezone(UTC)


@dataclass(frozen=True, slots=True, init=False)
class VerifiedRuntimeProfile:
    """A server-signed profile: the only type the gate accepts as proof.

    Only ``parse_runtime_profile`` can produce instances (via the module
    private token); a bare ``RuntimeProfile`` dataclass is explicitly not a
    verification proof and is rejected by the gate (P0-1).
    """

    profile: RuntimeProfile
    signature: str
    wire_payload: Mapping[str, object]

    def __init__(
        self,
        profile: RuntimeProfile,
        signature: str,
        _token: object,
        wire_payload: Mapping[str, object] | None = None,
    ) -> None:
        if _token is not _VERIFIED_TOKEN:
            raise TypeError(
                "VerifiedRuntimeProfile can only be created by parse_runtime_profile"
            )
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "signature", signature)
        object.__setattr__(
            self,
            "wire_payload",
            MappingProxyType(dict(wire_payload or {})),
        )

    def wire_payload_copy(self) -> dict[str, object]:
        """Return the exact signed wire envelope for a downstream authority."""

        return dict(self.wire_payload)


def verify_runtime_profile_signature(
    payload: Mapping[str, object],
    verify_key: str | None,
) -> bool:
    """Verify the backend HMAC-SHA256 signature over the canonical wire payload.

    Fail-closed: a missing/blank key, a missing signature, missing canonical
    fields or any mismatch rejects the profile.  The canonical bytes are the
    complete wire payload minus ``signature`` (nested ``persona`` included),
    serialised exactly as the server signs it (see module doc); no unsigned
    field can be smuggled in because it would change those bytes.
    """

    if not isinstance(verify_key, str) or not verify_key.strip():
        return False
    raw_signature = payload.get("signature")
    if not isinstance(raw_signature, str) or not raw_signature.strip():
        return False
    if payload.get("signature_schema") not in SUPPORTED_SIGNATURE_SCHEMAS:
        return False
    canonical = {key: value for key, value in payload.items() if key != "signature"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(verify_key.strip().encode("utf-8"), encoded, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, raw_signature.strip().lower())


def _parse_utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a UTC timestamp string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a UTC timestamp string") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must carry a timezone")
    return parsed.astimezone(UTC)


def parse_runtime_profile(
    payload: object,
    *,
    now: datetime | None = None,
    verify_key: str | None = None,
) -> VerifiedRuntimeProfile | None:
    """Parse and verify a signed RuntimeProfile; any failure returns None.

    Fail-closed by construction: the caller can never distinguish "absent"
    from "invalid", so unknown_safe is the only possible degradation.  The
    HMAC signature is verified before any field is trusted; without a verify
    key (or with a missing signature field) the profile is rejected.  The
    wire payload is then validated against the generated
    ``RuntimeProfileSigned`` contract (extra fields forbidden, strict types,
    RFC3339 datetimes, unique capability/obligation/receipt lists,
    ``subject_revision >= 0``), followed by the business cross-invariants:
    confirmed speaker iff a subject is named; unknown_safe only carries safe
    capabilities and must include DO_NOT_PERSIST; minors never carry
    adult-only capabilities or service modes (P0-1/P0-2).  The returned
    ``VerifiedRuntimeProfile`` is the only accepted proof type.
    """

    if not isinstance(payload, Mapping):
        return None
    if not verify_runtime_profile_signature(payload, verify_key):
        return None
    # Strict numeric pre-check: Pydantic StrictInt accepts numeric strings in
    # some versions, so the wire types are enforced here before validation.
    for field in ("session_epoch", "binding_version", "subject_revision"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
    confidence = payload.get("speaker_confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        return None
    persona = payload.get("persona")
    persona_version = (
        persona.get("version") if isinstance(persona, Mapping) else None
    )
    if isinstance(persona_version, bool) or not isinstance(persona_version, int):
        return None
    try:
        signature_schema = payload.get("signature_schema")
        signed: RuntimeProfileSigned | RuntimeProfileSignedV2
        if signature_schema == RUNTIME_PROFILE_V2_SIGNATURE_SCHEMA:
            signed = RuntimeProfileSignedV2.model_validate(payload)
        elif signature_schema == SIGNATURE_SCHEMA:
            signed = RuntimeProfileSigned.model_validate(payload)
        else:
            return None
        category: CanonicalSubjectCategory = signed.subject_category.value
        if category == "minor" and signed.age_band.value not in {"under_14", "14_17"}:
            return None
        if category == "adult" and signed.age_band.value != "adult":
            return None
        mode = signed.service_mode.value
        speaker = signed.speaker_state.value
        subject = signed.active_subject_id
        if category == "unknown" and mode != "unknown_safe":
            # Never guess the subject or rewrite the signed mode: an unknown
            # category with a non-safe mode is a signed inconsistency (audit 2).
            return None
        if (speaker == "confirmed") != (subject is not None):
            # P0-2: confirmed iff a subject is named; a named subject with an
            # unconfirmed speaker (or vice versa) is a signed inconsistency.
            return None
        capabilities = tuple(item.value for item in signed.capabilities)
        obligations = (
            tuple(item.code.value for item in signed.obligations)
            if isinstance(signed, RuntimeProfileSignedV2)
            else tuple(item.value for item in signed.obligations)
        )
        if mode == "unknown_safe":
            # Unknown-safe never rewrites the signed payload: capabilities
            # must be a (possibly empty) subset of the safe surface and the
            # canonical safe obligations must all be present (audit 2).
            if not set(capabilities) <= UNKNOWN_SAFE_CAPABILITIES:
                return None
            if not set(obligations) >= UNKNOWN_SAFE_REQUIRED_OBLIGATIONS:
                return None
        if category == "minor" and (
            mode in ADULT_ONLY_SERVICE_MODES
            or any(item in MINOR_FORBIDDEN_CAPABILITIES for item in capabilities)
        ):
            return None
        if category == "adult" and mode == "student_minor":
            return None
        if mode in ADULT_ONLY_SERVICE_MODES and category not in {"adult", "unknown"}:
            return None
        if mode == "unknown_safe" and category == "adult":
            return None
        profile = RuntimeProfile(
            runtime_profile_id=signed.runtime_profile_id,
            actor_id=signed.actor_id,
            binding_id=signed.binding_id,
            binding_version=signed.binding_version,
            subject_revision=signed.subject_revision,
            active_subject_id=subject,
            subject_category=category,
            age_band=signed.age_band.value,
            speaker_state=cast(Literal["unknown", "unconfirmed", "confirmed"], speaker),
            speaker_confidence=signed.speaker_confidence,
            service_mode=mode,
            persona_assignment_id=signed.persona_assignment_id,
            persona_id=signed.persona.persona_id,
            persona_version=signed.persona.version,
            relationship_stage=signed.persona.relationship_stage,
            policy_bundle_version=signed.policy_bundle_version,
            policy_receipt_ids=tuple(signed.policy_receipt_ids),
            session_epoch=signed.session_epoch,
            issued_at=signed.issued_at,
            expires_at=signed.expires_at,
            session_id=signed.session_id,
            device_id=signed.device_id,
            capabilities=capabilities,
            obligations=obligations,
        )
        if profile.is_expired(now or datetime.now(UTC)):
            return None
        signature = payload.get("signature")
        return VerifiedRuntimeProfile(
            profile=profile,
            signature=str(signature).strip() if isinstance(signature, str) else "",
            wire_payload=payload,
            _token=_VERIFIED_TOKEN,
        )
    except (ValueError, TypeError):
        return None


def is_expired_profile_payload(payload: object, *, now: datetime | None = None) -> bool:
    """Report whether a payload is structurally a profile that is expired.

    Used only for bounded trust metrics so the Agent can distinguish
    "expired" from "malformed" without ever exposing either to consumers.
    """

    if not isinstance(payload, Mapping):
        return False
    for field in ("runtime_profile_id",):
        if not isinstance(payload.get(field), str) or not str(payload.get(field)).strip():
            return False
    if not isinstance(payload.get("service_mode"), str):
        return False
    try:
        expires_at = _parse_utc_timestamp(payload.get("expires_at"), "expires_at")
    except ValueError:
        return False
    return expires_at <= (now or datetime.now(UTC)).astimezone(UTC)


def subject_context_from_profile(
    profile: RuntimeProfile | None,
) -> Mapping[str, object]:
    """Build the minimal allowlisted subject context from a profile only.

    Account owner / voice-speaker decisions never enter this mapping: with no
    profile the subject is unknown and unconfirmed (unknown_safe).
    """

    if profile is None:
        return {"subject_category": "unknown", "is_confirmed": False}
    return {
        "subject_category": profile.subject_category,
        "is_confirmed": profile.speaker_state == "confirmed",
    }


__all__ = [
    "RuntimeProfile",
    "RUNTIME_PROFILE_V2_SIGNATURE_SCHEMA",
    "SIGNATURE_SCHEMA",
    "SUPPORTED_SIGNATURE_SCHEMAS",
    "UNKNOWN_SAFE_CAPABILITIES",
    "UNKNOWN_SAFE_REQUIRED_OBLIGATIONS",
    "VerifiedRuntimeProfile",
    "VERIFY_KEY_ENV",
    "is_expired_profile_payload",
    "parse_runtime_profile",
    "subject_context_from_profile",
    "verify_runtime_profile_signature",
]
