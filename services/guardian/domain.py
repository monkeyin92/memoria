"""Pure guardian-link, consent, and account-subject contracts.

The account category is the root input to every minor capability decision.  It
therefore lives beside the guardian domain rather than in any individual API
route.  This module has no framework or persistence dependencies so the same
ratchet is reusable by SQLite profile persistence, PostgreSQL guardian storage,
and offline workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    AgeEvidenceStatusValue,
    SubjectCategoryValue,
)

type SubjectCategory = SubjectCategoryValue
type BirthYearBand = AgeBandValue
type AgeEvidenceStatus = AgeEvidenceStatusValue
Relation = Literal["parent", "legal_guardian"]
GuardianLinkStatus = Literal["pending", "active", "revoked"]
VerifiedVia = Literal["wechat_identity", "manual_review"]
ConsentKind = Literal[
    "minor_voice_session",
    "memory_retention",
    "weekly_report",
    "corpus_recording",
]


class GuardianNotFoundError(LookupError):
    """The requested guardian-scoped resource does not exist."""


class GuardianAccessDeniedError(PermissionError):
    """The actor is not allowed to operate on the guardian relationship."""


class GuardianConflictError(RuntimeError):
    """A guardian link or consent changed concurrently or violates a lifecycle rule."""


class SubjectTransitionError(ValueError):
    """An account-category transition violates the one-way minor ratchet."""


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, *, field: str, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return normalized


def validate_subject_transition(
    *,
    current_category: SubjectCategory,
    current_birth_year_band: BirthYearBand,
    current_age_evidence_status: AgeEvidenceStatus,
    target_category: SubjectCategory,
    target_birth_year_band: BirthYearBand,
    target_age_evidence_status: AgeEvidenceStatus,
    age_eligible: bool = False,
    guardian_confirmed: bool = False,
) -> None:
    """Validate a policy-relevant profile update.

    ``unknown -> adult`` requires verified adult evidence. ``minor -> adult``
    additionally requires age eligibility and guardian confirmation. Merely
    changing an age band never upgrades capabilities.
    """

    if current_category not in {"unknown", "adult", "minor"}:
        raise SubjectTransitionError("current subject category is invalid")
    if target_category not in {"unknown", "adult", "minor"}:
        raise SubjectTransitionError("target subject category is invalid")
    valid_bands = {"unknown", "under_14", "14_17", "adult"}
    if current_birth_year_band not in valid_bands:
        raise SubjectTransitionError("current birth year band is invalid")
    if target_birth_year_band not in valid_bands:
        raise SubjectTransitionError("target birth year band is invalid")
    valid_evidence = {"unverified", "verified", "disputed"}
    if current_age_evidence_status not in valid_evidence:
        raise SubjectTransitionError("current age evidence status is invalid")
    if target_age_evidence_status not in valid_evidence:
        raise SubjectTransitionError("target age evidence status is invalid")
    if target_category == "unknown" and target_birth_year_band != "unknown":
        raise SubjectTransitionError("unknown subjects require an unknown age band")
    if target_category == "minor" and target_birth_year_band not in {
        "under_14",
        "14_17",
    }:
        raise SubjectTransitionError("minor accounts require a minor age band")
    if target_category == "adult" and (
        target_birth_year_band != "adult"
        or target_age_evidence_status != "verified"
    ):
        raise SubjectTransitionError(
            "adult migration requires verified adult age evidence"
        )
    if current_category == "minor" and target_category == "adult":
        if not age_eligible or not guardian_confirmed:
            raise SubjectTransitionError(
                "minor to adult migration requires age eligibility and guardian confirmation"
            )
    if (
        current_category == "minor"
        and target_category == "minor"
        and target_birth_year_band == "adult"
    ):
        raise SubjectTransitionError("an adult age band cannot remain a minor profile")


@dataclass(frozen=True, slots=True)
class GuardianLink:
    link_id: str
    guardian_user_id: str
    minor_user_id: str
    relation: Relation
    status: GuardianLinkStatus
    verified_via: VerifiedVia
    created_at: datetime
    binding_expires_at: datetime
    activated_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("link_id", "guardian_user_id", "minor_user_id"):
            object.__setattr__(self, field, _bounded(getattr(self, field), field=field))
        if self.guardian_user_id == self.minor_user_id:
            raise ValueError("guardian and minor accounts must differ")
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))
        object.__setattr__(
            self,
            "binding_expires_at",
            _utc(self.binding_expires_at, field="binding_expires_at"),
        )
        if self.binding_expires_at <= self.created_at:
            raise ValueError("binding code expiry must follow link creation")
        if self.activated_at is not None:
            object.__setattr__(
                self,
                "activated_at",
                _utc(self.activated_at, field="activated_at"),
            )
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _utc(self.revoked_at, field="revoked_at"))
        if self.status == "pending" and (
            self.activated_at is not None or self.revoked_at is not None
        ):
            raise ValueError("pending guardian links cannot be activated or revoked")
        if self.status == "active" and (
            self.activated_at is None or self.revoked_at is not None
        ):
            raise ValueError("active guardian links require activation without revocation")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked guardian links require revoked_at")


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    consent_id: str
    link_id: str
    consent_kind: ConsentKind
    policy_version: str
    granted_at: datetime
    evidence_event_id: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_evidence_event_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("consent_id", "link_id", "policy_version", "evidence_event_id"):
            maximum = 64 if field == "policy_version" else 128
            object.__setattr__(
                self,
                field,
                _bounded(getattr(self, field), field=field, maximum=maximum),
            )
        object.__setattr__(self, "granted_at", _utc(self.granted_at, field="granted_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, field="expires_at"))
            if self.expires_at <= self.granted_at:
                raise ValueError("consent expiry must follow its grant")
        if self.consent_kind == "corpus_recording":
            if self.expires_at is None:
                raise ValueError("corpus recording consent requires an expiry")
            if self.expires_at - self.granted_at > timedelta(days=30):
                raise ValueError("corpus recording consent cannot exceed 30 days")
        elif self.expires_at is not None:
            raise ValueError("only corpus recording consent may expire")
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _utc(self.revoked_at, field="revoked_at"))
            if self.revocation_evidence_event_id is None:
                raise ValueError("revoked consent requires revocation evidence")
        if self.revocation_evidence_event_id is not None:
            object.__setattr__(
                self,
                "revocation_evidence_event_id",
                _bounded(
                    self.revocation_evidence_event_id,
                    field="revocation_evidence_event_id",
                ),
            )
            if self.revoked_at is None:
                raise ValueError("revocation evidence requires revoked_at")

    @property
    def active(self) -> bool:
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > datetime.now(UTC)
        )


@dataclass(frozen=True, slots=True)
class PersonConsentRecord:
    """One person-scoped guardian consent for a subject with no account.

    The adult who owns the ACTIVE ``parent_for_child`` binding naming the
    subject is the grantor; the subject never confirms anything and no
    guardian link is manufactured.  This is a distinct record type from
    ``ConsentRecord`` so a link-scoped consent can never be confused with a
    binding-scoped one, and so the read gate can union both without making
    a ``link_id`` mandatory.
    """

    consent_id: str
    subject_person_id: str
    grantor_person_id: str
    consent_kind: ConsentKind
    policy_version: str
    granted_at: datetime
    evidence_event_id: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_evidence_event_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "consent_id",
            "subject_person_id",
            "grantor_person_id",
            "policy_version",
            "evidence_event_id",
        ):
            maximum = 64 if field == "policy_version" else 128
            object.__setattr__(
                self,
                field,
                _bounded(getattr(self, field), field=field, maximum=maximum),
            )
        if self.subject_person_id == self.grantor_person_id:
            raise ValueError("person consent requires a distinct grantor and subject")
        object.__setattr__(self, "granted_at", _utc(self.granted_at, field="granted_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, field="expires_at"))
            if self.expires_at <= self.granted_at:
                raise ValueError("consent expiry must follow its grant")
        if self.consent_kind == "corpus_recording":
            if self.expires_at is None:
                raise ValueError("corpus recording consent requires an expiry")
            if self.expires_at - self.granted_at > timedelta(days=30):
                raise ValueError("corpus recording consent cannot exceed 30 days")
        elif self.expires_at is not None:
            raise ValueError("only corpus recording consent may expire")
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _utc(self.revoked_at, field="revoked_at"))
            if self.revocation_evidence_event_id is None:
                raise ValueError("revoked consent requires revocation evidence")
        if self.revocation_evidence_event_id is not None:
            object.__setattr__(
                self,
                "revocation_evidence_event_id",
                _bounded(
                    self.revocation_evidence_event_id,
                    field="revocation_evidence_event_id",
                ),
            )
            if self.revoked_at is None:
                raise ValueError("revocation evidence requires revoked_at")

    @property
    def active(self) -> bool:
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > datetime.now(UTC)
        )


def same_person_consent_grant_request(
    current: PersonConsentRecord,
    requested: PersonConsentRecord,
) -> bool:
    """Whether a retry of the same person-consent grant is being replayed.

    ``granted_at`` (and therefore the absolute ``expires_at``) is recomputed
    by every HTTP attempt, so an idempotent retry cannot be recognized by full
    record equality.  The stable request identity is the deterministic consent
    id plus grantor/subject/kind/policy and the requested retention span; a
    different payload under the same id still conflicts.
    """

    current_span = (
        current.expires_at - current.granted_at
        if current.expires_at is not None
        else None
    )
    requested_span = (
        requested.expires_at - requested.granted_at
        if requested.expires_at is not None
        else None
    )
    return (
        current.consent_id == requested.consent_id
        and current.subject_person_id == requested.subject_person_id
        and current.grantor_person_id == requested.grantor_person_id
        and current.consent_kind == requested.consent_kind
        and current.policy_version == requested.policy_version
        and current.evidence_event_id == requested.evidence_event_id
        and current_span == requested_span
    )


class GuardianStorePort(Protocol):
    async def create_link(
        self,
        *,
        link_id: str | None = None,
        guardian_user_id: str,
        minor_user_id: str,
        relation: Relation,
        verified_via: VerifiedVia,
        binding_code_hash: str,
        binding_expires_at: datetime,
        now: datetime,
    ) -> GuardianLink: ...

    async def verify_binding_code(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink: ...

    async def confirm_link(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink: ...

    async def get_link(self, *, link_id: str, actor_user_id: str) -> GuardianLink: ...

    async def active_link(
        self,
        *,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> GuardianLink | None: ...

    async def list_links(
        self,
        *,
        guardian_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]: ...

    async def list_links_for_actor(
        self,
        *,
        actor_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]: ...

    async def active_guardian_links(
        self,
        *,
        minor_user_id: str,
    ) -> tuple[GuardianLink, ...]: ...

    async def grant_consent(
        self,
        record: ConsentRecord,
        *,
        actor_user_id: str | None = None,
    ) -> ConsentRecord: ...

    async def get_consent(
        self,
        *,
        consent_id: str,
        actor_user_id: str,
    ) -> ConsentRecord: ...

    async def list_consents(
        self,
        *,
        link_id: str,
        actor_user_id: str,
    ) -> tuple[ConsentRecord, ...]: ...

    async def revoke_consent(
        self,
        *,
        consent_id: str,
        guardian_user_id: str,
        revoked_at: datetime,
        revocation_evidence_event_id: str,
    ) -> ConsentRecord: ...

    async def active_consent(
        self,
        *,
        minor_user_id: str,
        consent_kind: ConsentKind,
    ) -> ConsentRecord | PersonConsentRecord | None:
        """Union over link-scoped and person-scoped consent key spaces.

        Implementations return either record type because an account-less
        subject can never confirm a guardian link; consumers use the common
        fields (``consent_id``/``consent_kind``/``policy_version``/
        ``expires_at``) and must not assume a ``link_id`` exists.
        """
        ...

    # -- person-scoped consents (account-less subjects) ------------------

    async def grant_person_consent(
        self,
        record: PersonConsentRecord,
        *,
        actor_person_id: str,
    ) -> PersonConsentRecord: ...

    async def revoke_person_consent(
        self,
        *,
        consent_id: str,
        grantor_person_id: str,
        subject_person_id: str,
        revoked_at: datetime,
        revocation_evidence_event_id: str,
    ) -> PersonConsentRecord: ...

    async def get_person_consent(
        self,
        *,
        consent_id: str,
        actor_person_id: str,
        subject_person_id: str,
    ) -> PersonConsentRecord:
        """Read one person consent under a trusted subject context.

        ``subject_person_id`` is not derived from the row (the caller must
        already be authorized for that subject); storage uses it as the
        subject scope so a grantor context can satisfy FORCE RLS without
        seeing another subject's rows.
        """
        ...

    async def list_person_consents(
        self,
        *,
        subject_person_id: str,
        actor_person_id: str,
    ) -> tuple[PersonConsentRecord, ...]: ...

    async def active_person_consent(
        self,
        *,
        subject_person_id: str,
        consent_kind: ConsentKind,
    ) -> PersonConsentRecord | None: ...

    async def export_for_account(self, *, account_id: str) -> dict[str, object]: ...

    async def delete_for_account(self, *, account_id: str) -> dict[str, int]: ...

    async def remaining_account_rows(self, *, account_id: str) -> dict[str, int]: ...

    async def related_minor_accounts(self, *, guardian_user_id: str) -> tuple[str, ...]: ...
