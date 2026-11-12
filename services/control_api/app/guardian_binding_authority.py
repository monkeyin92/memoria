"""Guardian binding authority for person-level consents.

A person-level grant is only valid when the login account owns an ACTIVE
``parent_for_child`` binding that names the subject. The same binding decides
whether an accountless child counts as guarded, and every consent toggle must
move both the Guardian ledger and the consent authority that Policy reads.
Those rules live here; the guardian router takes the request and calls them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from fastapi import HTTPException, Request
from packages.contracts.generated.python.multi_subject_contracts import CapabilityValue
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.consent.bound_subject import (
    GUARDIAN_MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectConsentService,
    BoundSubjectGrant,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import AuthenticatedUser
from services.guardian.domain import ConsentKind, PersonConsentRecord
from services.identity.service import IdentityService

__all__ = [
    "GuardianConsentCreate",
    "owns_accountless_child",
    "person_grant_body_matches",
    "require_binding_owner_for_subject",
    "sync_bound_subject_authority",
]


def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


class GuardianConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    consent_kind: ConsentKind
    policy_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    retention_days: int | None = Field(default=None, ge=1, le=30)

    @model_validator(mode="after")
    def validate_corpus_retention(self) -> GuardianConsentCreate:
        if self.consent_kind == "corpus_recording" and self.retention_days is None:
            raise ValueError("corpus recording consent requires retention_days")
        if self.consent_kind != "corpus_recording" and self.retention_days is not None:
            raise ValueError("retention_days is only valid for corpus recording consent")
        return self


def person_grant_body_matches(
    record: PersonConsentRecord,
    body: GuardianConsentCreate,
) -> bool:
    """Whether a replayed idempotency key carries the same grant request.

    Absolute timestamps are recomputed per attempt, so the request is matched
    on the grantor/subject/kind/policy identity plus the requested retention
    span; a different payload under the same key conflicts.
    """

    if (
        record.consent_kind != body.consent_kind
        or record.policy_version != body.policy_version
    ):
        return False
    if body.retention_days is None:
        return record.expires_at is None
    return (
        record.expires_at is not None
        and record.expires_at - record.granted_at
        == timedelta(days=body.retention_days)
    )


async def require_binding_owner_for_subject(
    request: Request,
    *,
    user: AuthenticatedUser,
    subject_person_id: str,
) -> None:
    """Authorize a person-level consent against the device binding.

    The only accepted grantor is the account owner of an ACTIVE
    ``parent_for_child`` binding that names this subject as its primary
    subject — the same binding-scoped authority that makes a guardian
    declaration count.  A subject without such a binding is refused: nobody
    may grant capabilities over a child they do not actually manage.
    """

    identity = getattr(request.app.state, "identity_service", None)
    if not isinstance(identity, IdentityService):
        raise HTTPException(
            status_code=503,
            detail={"code": "identity_authority_unavailable"},
        )
    manifests = await identity.list_active_manifests_for_person(
        subject_person_id,
        actor_person_id=user.user_id,
    )
    if not any(
        manifest.declared_mode == "parent_for_child"
        and manifest.account_owner_id == user.user_id
        and subject_person_id in manifest.primary_subject_ids
        for manifest in manifests
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "guardian_binding_owner_required"},
        )


#: Guardian consent kinds that stand for standing grants in the consent
#: authority Policy reads (see ``services.consent.bound_subject``).
_BOUND_SUBJECT_CAPABILITIES: dict[str, tuple[CapabilityValue, ...]] = {
    "memory_retention": GUARDIAN_MEMORY_CAPABILITIES,
    "minor_voice_session": MINOR_SESSION_CAPABILITIES,
}


async def sync_bound_subject_authority(
    request: Request,
    *,
    user: AuthenticatedUser,
    subject_person_id: str,
    consent_kind: str,
    granted: bool,
    source_key: str,
) -> None:
    """Mirror a person-level guardian consent into the consent authority.

    The retention ceiling reads the Guardian ledger and Policy reads the
    consent authority; one toggle must move both, on every active binding
    this guardian owns for the child. Without a consent authority (SQLite
    deployments have no Session Runtime) there is nothing to mirror.
    """

    await sync_person_consent_authority(
        identity=getattr(request.app.state, "identity_service", None),
        service=getattr(request.app.state, "bound_subject_consent", None),
        actor_id=user.user_id, subject_person_id=subject_person_id,
        consent_kind=consent_kind, granted=granted, source_key=source_key,
    )


async def sync_person_consent_authority(*, identity: IdentityService | None,
    service: BoundSubjectConsentService | None, actor_id: str, subject_person_id: str,
    consent_kind: str, granted: bool, source_key: str) -> None:
    capabilities = _BOUND_SUBJECT_CAPABILITIES.get(consent_kind)
    if capabilities is None or service is None or identity is None:
        return
    manifests = await identity.list_active_manifests_for_person(
        subject_person_id,
        actor_person_id=actor_id,
    )
    for manifest in manifests:
        if (
            manifest.declared_mode != "parent_for_child"
            or manifest.account_owner_id != actor_id
            or subject_person_id not in manifest.primary_subject_ids
        ):
            continue
        if granted:
            await service.grant(
                BoundSubjectGrant(
                    actor_person_id=actor_id,
                    subject_person_id=subject_person_id,
                    binding_id=manifest.binding_id,
                    kind="guardian",
                    capabilities=capabilities,
                    source_key=source_key,
                )
            )
        else:
            await service.revoke(
                actor_person_id=actor_id,
                subject_person_id=subject_person_id,
                binding_id=manifest.binding_id,
                capabilities=capabilities,
                reason=f"guardian_{consent_kind}_revoked",
            )


async def owns_accountless_child(
    request: Request,
    *,
    user: AuthenticatedUser,
    subject_person_id: str,
) -> bool:
    """Whether this guardian binds a device for this child, who has no account."""

    if _profiles(request).get_account(user_id=subject_person_id) is not None:
        return False
    try:
        await require_binding_owner_for_subject(
            request, user=user, subject_person_id=subject_person_id
        )
    except HTTPException:
        return False
    return True
