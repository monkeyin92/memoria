from __future__ import annotations

from services.consent.bound_subject import BoundSubjectConsentService
from services.control_api.app.guardian_binding_authority import sync_person_consent_authority
from services.control_api.app.session_termination import AccountSessionTerminator
from services.guardian.consent import ConsentRevocation, ConsentRevocationHook
from services.identity.service import IdentityService


def guardian_revocation_hook(
    terminator: AccountSessionTerminator,
    *,
    identity: IdentityService,
    bound_consent: BoundSubjectConsentService | None,
) -> ConsentRevocationHook:
    async def on_revoked(event: ConsentRevocation) -> None:
        if event.scope == "bound_subject":
            await sync_person_consent_authority(
                identity=identity,
                service=bound_consent,
                actor_id=event.grantor_person_id,
                subject_person_id=event.subject_person_id,
                consent_kind=event.kind,
                granted=False,
                source_key="guardian_revocation",
            )
        if event.kind == "minor_voice_session":
            await terminator.terminate_subject(
                event.subject_person_id,
                actor_id=event.grantor_person_id,
                reason="consent_withdrawn",
            )
        if event.scope == "minor_account" and event.kind in {
            "minor_voice_session",
            "memory_retention",
        }:
            await terminator.terminate_account(event.subject_person_id)

    return on_revoked
