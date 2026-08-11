"""Readers and row mappers for the four legacy consent sources.

The old modules (``services.guardian``, ``services.voice_profile``,
``services.archive``, ``services.persona``) are read-only imports; nothing here
modifies them.  Each source exposes a reader ``Protocol`` (Port) and a thin
adapter that maps source rows into the uniform :class:`RawLegacyConsent` row.
Real database readers are intentionally NOT wired here: the Ports are the
wiring boundary for the deployment phase.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.guardian.domain import ConsentRecord, GuardianLink


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware (UTC or offset)")


@dataclass(frozen=True, slots=True)
class RawLegacyConsent:
    """Uniform, source-neutral legacy consent row (all fields are clues, not authority).

    ``subject_clue`` / ``actor_clue`` are the identities the old source can
    prove; ``subject_account_clue`` keeps the account-level identity when a
    person-level subject is not provable (e.g. persona rows).  ``binding_clue``
    and ``binding_status`` carry guardian-link information.  ``raw`` is the
    original source object for audit; it is never part of canonical digests.
    """

    source: str
    legacy_id: str
    subject_clue: str
    subject_account_clue: str | None
    actor_clue: str
    capability_clue: str
    purpose_clue: str
    evidence_clue: str
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    policy_version: str
    binding_clue: str | None
    binding_status: str | None
    raw: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.legacy_id.strip():
            raise ValueError("source and legacy_id must be non-empty")
        _require_aware(self.granted_at, field="granted_at")
        if self.expires_at is not None:
            _require_aware(self.expires_at, field="expires_at")
        if self.revoked_at is not None:
            _require_aware(self.revoked_at, field="revoked_at")


@dataclass(frozen=True, slots=True)
class GuardianConsentRow:
    """A guardian ``ConsentRecord`` plus the link that names subject and actor."""

    record: ConsentRecord
    link: GuardianLink | None


class GuardianConsentRowSource(Protocol):
    """Read-only source of guardian consent rows."""

    def iter_rows(self) -> Iterator[GuardianConsentRow]: ...


@dataclass(frozen=True, slots=True)
class VoiceCloneConsentRow:
    """Row shape of ``voice_clone_consents`` (see voice_profile/postgres_schema.sql)."""

    account_id: str
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None
    grant_event_id: str
    revoke_event_id: str | None


class VoiceCloneConsentRowSource(Protocol):
    """Read-only source of voice-clone consent rows."""

    def iter_rows(self) -> Iterator[VoiceCloneConsentRow]: ...


@dataclass(frozen=True, slots=True)
class RawAudioConsentRow:
    """Row shape of ``archive_consent_grants`` (see archive/postgres_schema.sql)."""

    consent_grant_id: str
    account_id: str
    policy_version: str
    retention_policy: str
    purpose: str | None
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    evidence_event_id: str | None


class RawAudioConsentRowSource(Protocol):
    """Read-only source of raw-audio consent rows."""

    def iter_rows(self) -> Iterator[RawAudioConsentRow]: ...


@dataclass(frozen=True, slots=True)
class PersonaLearningConsentRow:
    """Row shape of ``persona_learning_consents`` (see persona/postgres_schema.sql).

    The legacy table is account-scoped and carries no person identity; a
    ``person_id`` may be supplied by an enriched reader when an authoritative
    account-to-person mapping exists.
    """

    account_id: str
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None
    grant_event_id: str
    revoke_event_id: str | None
    person_id: str | None = None


class PersonaLearningConsentRowSource(Protocol):
    """Read-only source of persona-learning consent rows."""

    def iter_rows(self) -> Iterator[PersonaLearningConsentRow]: ...


def map_guardian_row(row: GuardianConsentRow) -> RawLegacyConsent:
    """Map a guardian consent record + link into a uniform legacy row."""
    record = row.record
    link = row.link
    return RawLegacyConsent(
        source="guardian",
        legacy_id=record.consent_id,
        subject_clue=link.minor_user_id if link is not None else "",
        subject_account_clue=None,
        actor_clue=link.guardian_user_id if link is not None else "",
        capability_clue=record.consent_kind,
        purpose_clue=record.consent_kind,
        evidence_clue=record.evidence_event_id,
        granted_at=record.granted_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        policy_version=record.policy_version,
        binding_clue=link.link_id if link is not None else None,
        binding_status=link.status if link is not None else None,
        raw={"consent_id": record.consent_id, "record": record, "link": link},
    )


def map_voice_clone_row(row: VoiceCloneConsentRow) -> RawLegacyConsent:
    """Map a voice-clone consent row (self-granted by the account owner)."""
    return RawLegacyConsent(
        source="voice_clone",
        legacy_id=row.account_id,
        subject_clue=row.account_id,
        subject_account_clue=None,
        actor_clue=row.account_id,
        capability_clue="voice_clone",
        purpose_clue="voice_clone",
        evidence_clue=row.grant_event_id,
        granted_at=row.granted_at,
        expires_at=None,
        revoked_at=row.revoked_at,
        policy_version=row.policy_version,
        binding_clue=None,
        binding_status=None,
        raw={
            "account_id": row.account_id,
            "grant_event_id": row.grant_event_id,
            "revoke_event_id": row.revoke_event_id,
        },
    )


def map_raw_audio_row(row: RawAudioConsentRow) -> RawLegacyConsent:
    """Map a raw-audio consent grant row (self-granted by the account owner)."""
    return RawLegacyConsent(
        source="raw_audio",
        legacy_id=row.consent_grant_id,
        subject_clue=row.account_id,
        subject_account_clue=None,
        actor_clue=row.account_id,
        capability_clue="raw_audio_retention",
        purpose_clue=row.purpose or "raw_audio_retention",
        evidence_clue=row.evidence_event_id or "",
        granted_at=row.granted_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        policy_version=row.policy_version,
        binding_clue=None,
        binding_status=None,
        raw={
            "consent_grant_id": row.consent_grant_id,
            "account_id": row.account_id,
            "retention_policy": row.retention_policy,
            "evidence_event_id": row.evidence_event_id,
        },
    )


def map_persona_row(row: PersonaLearningConsentRow) -> RawLegacyConsent:
    """Map a persona-learning consent row.

    The legacy table only proves an account; a person-level subject is only
    claimed when the row carries an authoritative ``person_id``.  Otherwise the
    account identity is kept in ``subject_account_clue`` and the normalizer
    quarantines the row (fail closed).
    """
    person = row.person_id
    return RawLegacyConsent(
        source="persona",
        legacy_id=row.account_id,
        subject_clue=person or "",
        subject_account_clue=None if person else row.account_id,
        actor_clue=person or "",
        capability_clue="persona_learning",
        purpose_clue="persona_learning",
        evidence_clue=row.grant_event_id,
        granted_at=row.granted_at,
        expires_at=None,
        revoked_at=row.revoked_at,
        policy_version=row.policy_version,
        binding_clue=None,
        binding_status=None,
        raw={
            "account_id": row.account_id,
            "grant_event_id": row.grant_event_id,
            "revoke_event_id": row.revoke_event_id,
            "person_id": person,
        },
    )


class GuardianConsentAdapter:
    """Adapter that maps guardian rows to :class:`RawLegacyConsent` rows."""

    def __init__(self, source: GuardianConsentRowSource) -> None:
        self._source = source

    def iter_rows(self) -> Iterator[RawLegacyConsent]:
        for row in self._source.iter_rows():
            yield map_guardian_row(row)


class VoiceCloneConsentAdapter:
    """Adapter that maps voice-clone rows to :class:`RawLegacyConsent` rows."""

    def __init__(self, source: VoiceCloneConsentRowSource) -> None:
        self._source = source

    def iter_rows(self) -> Iterator[RawLegacyConsent]:
        for row in self._source.iter_rows():
            yield map_voice_clone_row(row)


class RawAudioConsentAdapter:
    """Adapter that maps raw-audio rows to :class:`RawLegacyConsent` rows."""

    def __init__(self, source: RawAudioConsentRowSource) -> None:
        self._source = source

    def iter_rows(self) -> Iterator[RawLegacyConsent]:
        for row in self._source.iter_rows():
            yield map_raw_audio_row(row)


class PersonaConsentAdapter:
    """Adapter that maps persona-learning rows to :class:`RawLegacyConsent` rows."""

    def __init__(self, source: PersonaLearningConsentRowSource) -> None:
        self._source = source

    def iter_rows(self) -> Iterator[RawLegacyConsent]:
        for row in self._source.iter_rows():
            yield map_persona_row(row)
