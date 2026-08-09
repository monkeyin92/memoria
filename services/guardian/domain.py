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

SubjectCategory = Literal["adult", "minor"]
BirthYearBand = Literal["unknown", "under_14", "14_to_17", "18_or_over"]
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
    target_category: SubjectCategory,
    target_birth_year_band: BirthYearBand,
    age_eligible: bool = False,
    guardian_confirmed: bool = False,
) -> None:
    """Validate a policy-relevant profile update.

    ``adult -> minor`` is the corrective path used when a parent registers a
    child under the adult default.  ``minor -> adult`` is deliberately a
    one-way ratchet: both an age authority and the active guardian relationship
    must confirm the migration.  Merely changing the age band never upgrades
    capabilities.
    """

    if current_category not in {"adult", "minor"}:
        raise SubjectTransitionError("current subject category is invalid")
    if target_category not in {"adult", "minor"}:
        raise SubjectTransitionError("target subject category is invalid")
    valid_bands = {"unknown", "under_14", "14_to_17", "18_or_over"}
    if current_birth_year_band not in valid_bands:
        raise SubjectTransitionError("current birth year band is invalid")
    if target_birth_year_band not in valid_bands:
        raise SubjectTransitionError("target birth year band is invalid")
    if target_category == "minor" and target_birth_year_band not in {
        "under_14",
        "14_to_17",
    }:
        raise SubjectTransitionError("minor accounts require a minor age band")
    if current_category == "minor" and target_category == "adult":
        if target_birth_year_band != "18_or_over":
            raise SubjectTransitionError("adult migration requires the 18_or_over band")
        if not age_eligible or not guardian_confirmed:
            raise SubjectTransitionError(
                "minor to adult migration requires age eligibility and guardian confirmation"
            )
    if (
        current_category == "minor"
        and target_category == "minor"
        and target_birth_year_band == "18_or_over"
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

    async def grant_consent(self, record: ConsentRecord) -> ConsentRecord: ...

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
    ) -> ConsentRecord | None: ...

    async def export_for_account(self, *, account_id: str) -> dict[str, object]: ...

    async def delete_for_account(self, *, account_id: str) -> dict[str, int]: ...

    async def remaining_account_rows(self, *, account_id: str) -> dict[str, int]: ...

    async def related_minor_accounts(self, *, guardian_user_id: str) -> tuple[str, ...]: ...
