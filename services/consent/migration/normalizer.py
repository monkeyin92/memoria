"""Fail-closed classification of legacy consent rows.

A row becomes a :class:`NormalizedConsent` (offer candidate) only when every
proof requirement holds: subject determinable, purpose determinable, evidence
referencable, and actor rules satisfied (sensitive capabilities require
``actor == subject``).  Anything else becomes a :class:`QuarantineEntry` with an
explicit reason.  Old consents are never automatically turned into effective
grants; candidates must still pass the Consent Authority (Agent A) with fresh
binding/relationship evidence at the wiring boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from packages.contracts.generated.python.multi_subject_contracts import CapabilityValue

from services.consent.migration.adapters import RawLegacyConsent, _require_aware

# ---------------------------------------------------------------------------
# Capability policy tables (single decision point for the migration mapping)
# ---------------------------------------------------------------------------

#: Source capability clue -> canonical capability.  ``None`` means the source
#: capability has no automatic mapping and always quarantines for human review.
CAPABILITY_MAPPING: dict[str, CapabilityValue | None] = {
    # guardian consent kinds
    "memory_retention": "memory_capture",
    "weekly_report": "guardian_summary_view",
    "minor_voice_session": "chat",
    # Guardian corpus recording feeds model-training-adjacent voice material and
    # is guardian-granted; it must never auto-migrate.  Human confirmation only.
    "corpus_recording": None,
    # voice_profile
    "voice_clone": "voice_clone_use",
    # archive raw audio
    "raw_audio_retention": "raw_audio_retention",
    # persona learning: the old system treated this as a low-sensitivity
    # learning consent, but the canonical vocabulary only offers
    # ``model_training_contribution`` for data-usage learning.  Mapping is
    # deliberately conservative: the candidate is sensitive and therefore
    # requires actor == subject and re-validation by the Consent Authority.
    "persona_learning": "model_training_contribution",
}

#: Source purpose clue -> canonical purpose string for offer candidates.
#: Unknown clues pass through unchanged (the old system's purpose is the most
#: honest determinable purpose); a row with no purpose clue at all quarantines.
PURPOSE_MAPPING: dict[str, str] = {
    "memory_retention": "guardian_memory_retention",
    "weekly_report": "guardian_weekly_report",
    "minor_voice_session": "guardian_minor_voice_session",
    "corpus_recording": "guardian_corpus_recording",
    "voice_clone": "voice_clone_use",
    "raw_audio_retention": "raw_audio_retention",
    "persona_learning": "persona_learning",
}

#: Capabilities that only the subject themselves may grant (consent contract
#: rule 2).  Guardian/family_admin can never grant these.
SENSITIVE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "payment",
        "raw_audio_retention",
        "model_training_contribution",
        "device_ownership_transfer",
    }
)

#: Capabilities a guardian may grant for a minor (consent contract rule 3).
GUARDIAN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "chat",
        "tutor",
        "english_practice",
        "voice_profile_create",
        "memory_capture",
        "memory_recall_private",
        "guardian_summary_view",
    }
)


QuarantineReason = Literal[
    "missing_subject_proof",
    "missing_purpose",
    "missing_evidence",
    "actor_not_subject_for_sensitive",
    "ambiguous_binding",
    "expired_only",
    "unsupported_capability",
    "subject_unknown",
]


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def canonical_json(payload: object) -> str:
    """Deterministic JSON: sorted keys, compact separators (contract rule)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(payload: object) -> str:
    """sha256 hex over the canonical JSON of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_id(kind: str, *, source: str, legacy_id: str) -> str:
    key = f"memoria:legacy-{kind}:{source}:{legacy_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


@dataclass(frozen=True, slots=True)
class NormalizedConsent:
    """A provable legacy consent mapped to a canonical offer candidate.

    This is a candidate plus a deterministic old-id -> canonical-id mapping; it
    is NOT a grant.  ``canonical_hash`` covers the candidate fields in a fixed
    order and never includes the raw source object.
    """

    consent_id: str
    offer_id: str
    source: str
    legacy_id: str
    subject_id: str
    actor_id: str
    actor_kind: Literal["subject", "guardian"]
    resource_owner_id: str
    capability: CapabilityValue
    purpose: str
    evidence_ref: str
    policy_version: str
    granted_at: datetime
    expires_at: datetime | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """A legacy row that failed proof and must not migrate without human review."""

    quarantine_id: str
    source: str
    legacy_id: str
    reason: QuarantineReason
    detail: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    normalized: tuple[NormalizedConsent, ...]
    quarantined: tuple[QuarantineEntry, ...]


def _normalized_fields(
    *,
    source: str,
    legacy_id: str,
    subject_id: str,
    actor_id: str,
    actor_kind: str,
    resource_owner_id: str,
    capability: CapabilityValue,
    purpose: str,
    evidence_ref: str,
    policy_version: str,
    granted_at: datetime,
    expires_at: datetime | None,
) -> dict[str, object]:
    return {
        "kind": "normalized_consent",
        "source": source,
        "legacy_id": legacy_id,
        "subject_id": subject_id,
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "resource_owner_id": resource_owner_id,
        "capability": capability,
        "purpose": purpose,
        "evidence_ref": evidence_ref,
        "policy_version": policy_version,
        "granted_at": _iso(granted_at),
        "expires_at": _iso(expires_at),
    }


def _quarantine_fields(
    *,
    source: str,
    legacy_id: str,
    reason: QuarantineReason,
    detail: str,
) -> dict[str, object]:
    return {
        "kind": "quarantine_entry",
        "source": source,
        "legacy_id": legacy_id,
        "reason": reason,
        "detail": detail,
    }


def normalize_row(row: RawLegacyConsent, *, now: datetime) -> NormalizedConsent | QuarantineEntry:
    """Classify one legacy row, fail closed.

    Check order (deterministic, first failure wins):
    revoked/expired -> capability mapping -> guardian binding -> subject proof
    -> purpose -> evidence -> sensitive actor rule.
    """
    _require_aware(now, field="now")

    def quarantine(reason: QuarantineReason, detail: str) -> QuarantineEntry:
        return QuarantineEntry(
            quarantine_id=_canonical_id("quarantine", source=row.source, legacy_id=row.legacy_id),
            source=row.source,
            legacy_id=row.legacy_id,
            reason=reason,
            detail=detail,
            canonical_hash=canonical_digest(
                _quarantine_fields(
                    source=row.source,
                    legacy_id=row.legacy_id,
                    reason=reason,
                    detail=detail,
                )
            ),
        )

    if row.revoked_at is not None:
        return quarantine(
            "expired_only",
            f"consent revoked at {_iso(row.revoked_at)}; revocation is not a grant",
        )
    if row.expires_at is not None and row.expires_at <= now:
        return quarantine(
            "expired_only",
            f"consent expired at {_iso(row.expires_at)}; expiry is not a grant",
        )

    mapped_capability = CAPABILITY_MAPPING.get(row.capability_clue)
    if mapped_capability is None:
        if row.capability_clue == "corpus_recording":
            detail = (
                "guardian corpus recording is model-training-adjacent voice material; "
                "requires human confirmation and cannot migrate automatically"
            )
        else:
            detail = f"capability clue {row.capability_clue!r} has no canonical mapping"
        return quarantine("unsupported_capability", detail)

    if row.source == "guardian":
        if row.binding_clue is None:
            return quarantine("ambiguous_binding", "guardian link is missing")
        if row.binding_status != "active":
            return quarantine(
                "ambiguous_binding",
                f"guardian link status {row.binding_status!r} is not active",
            )

    if not row.subject_clue.strip():
        if row.subject_account_clue:
            return quarantine(
                "missing_subject_proof",
                f"subject is account {row.subject_account_clue!r} without person proof",
            )
        return quarantine("subject_unknown", "no subject identity clue")

    purpose_clue = row.purpose_clue.strip()
    if not purpose_clue:
        return quarantine("missing_purpose", "no deterministic purpose for this consent")
    purpose = PURPOSE_MAPPING.get(purpose_clue, purpose_clue)
    if not row.evidence_clue.strip():
        return quarantine("missing_evidence", "no referencable evidence event id")

    actor_id = row.actor_clue.strip()
    subject_id = row.subject_clue.strip()
    if mapped_capability in SENSITIVE_CAPABILITIES and actor_id != subject_id:
        return quarantine(
            "actor_not_subject_for_sensitive",
            f"actor {actor_id!r} differs from subject {subject_id!r} "
            f"for sensitive capability {mapped_capability}",
        )

    actor_kind: Literal["subject", "guardian"] = (
        "guardian" if row.source == "guardian" else "subject"
    )
    fields = _normalized_fields(
        source=row.source,
        legacy_id=row.legacy_id,
        subject_id=subject_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        resource_owner_id=subject_id,
        capability=mapped_capability,
        purpose=purpose,
        evidence_ref=row.evidence_clue.strip(),
        policy_version=row.policy_version,
        granted_at=row.granted_at,
        expires_at=row.expires_at,
    )
    return NormalizedConsent(
        consent_id=_canonical_id("consent", source=row.source, legacy_id=row.legacy_id),
        offer_id=_canonical_id("offer", source=row.source, legacy_id=row.legacy_id),
        source=row.source,
        legacy_id=row.legacy_id,
        subject_id=subject_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        resource_owner_id=subject_id,
        capability=mapped_capability,
        purpose=purpose,
        evidence_ref=row.evidence_clue.strip(),
        policy_version=row.policy_version,
        granted_at=row.granted_at,
        expires_at=row.expires_at,
        canonical_hash=canonical_digest(fields),
    )


def normalize_rows(
    rows: Iterable[RawLegacyConsent],
    *,
    now: datetime | None = None,
) -> NormalizationResult:
    """Classify all rows, sorted deterministically by (source, legacy_id)."""
    resolved_now = now if now is not None else datetime.now(UTC)
    _require_aware(resolved_now, field="now")
    normalized: list[NormalizedConsent] = []
    quarantined: list[QuarantineEntry] = []
    for row in rows:
        outcome = normalize_row(row, now=resolved_now)
        if isinstance(outcome, NormalizedConsent):
            normalized.append(outcome)
        else:
            quarantined.append(outcome)
    normalized.sort(key=lambda item: (item.source, item.legacy_id))
    quarantined.sort(key=lambda item: (item.source, item.legacy_id))
    return NormalizationResult(tuple(normalized), tuple(quarantined))
