"""Production Session + Identity authority adapter for MemoryScope."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    BindingRoleValue,
    ServiceModeValue,
    SpeakerStateValue,
    SubjectCategoryValue,
)

from services.control_api.app.multi_subject_runtime import (
    PostgresMultiSubjectRuntimeControl,
)
from services.identity.domain import IdentityAccessDeniedError
from services.memory_scope.domain import WriteFence
from services.memory_scope.repository import MemoryAuthoritySnapshot
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
)


class MemoryAuthorityUnavailable(RuntimeError):
    """The signed Session and active Identity manifest cannot be reconciled."""


class MemoryAuthorityDenied(PermissionError):
    """The actor or current speaker is not eligible for memory access."""


_ROLE_PRIORITY: tuple[str, ...] = (
    "primary_subject",
    "guardian",
    "account_owner",
    "delegate",
    "device_admin",
    "member",
    "emergency_contact",
)


class PostgresMemoryAuthority:
    """Resolve one immutable memory authority snapshot.

    Session Runtime reads profile + mutable context in one read transaction.
    Identity then returns the active manifest; every overlapping binding field
    is compared before any derived role/family/consent value is exposed.
    """

    def __init__(self, runtime: PostgresMultiSubjectRuntimeControl) -> None:
        self._runtime = runtime

    async def current(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> MemoryAuthoritySnapshot | None:
        try:
            profile, context = await self._runtime.sessions.current(
                actor_id=actor_id,
                session_id=session_id,
                now=now,
            )
        except PersistentSessionNotFound:
            return None
        except PersistentSessionDenied as exc:
            raise MemoryAuthorityDenied(str(exc)) from exc
        except PersistentSessionUnavailable as exc:
            raise MemoryAuthorityUnavailable(str(exc)) from exc

        if (
            context.actor_id != actor_id
            or context.session_id != session_id
            or profile.actor_id != actor_id
            or profile.session_id != session_id
            or context.device_id != profile.device_id
            or context.binding_id != profile.binding_id
            or context.binding_version != profile.binding_version
            or context.active_subject_id != profile.active_subject_id
            or context.subject_revision != profile.subject_revision
            or context.session_epoch != profile.session_epoch
            or context.current_runtime_profile_id != profile.runtime_profile_id
        ):
            raise MemoryAuthorityUnavailable(
                "Session profile and mutable context are inconsistent"
            )

        try:
            manifest = await self._runtime.require_binding_member(
                device_id=profile.device_id,
                user_id=actor_id,
                now=now,
            )
        except IdentityAccessDeniedError as exc:
            raise MemoryAuthorityDenied(str(exc)) from exc
        except Exception as exc:
            raise MemoryAuthorityUnavailable(
                "active Identity manifest is unavailable"
            ) from exc

        if (
            manifest.device_id != profile.device_id
            or manifest.binding_id != profile.binding_id
            or manifest.binding_version != profile.binding_version
            or manifest.policy_bundle_version != profile.policy_bundle_version
        ):
            raise MemoryAuthorityUnavailable(
                "Identity manifest does not match the signed Session profile"
            )

        active_subject = profile.active_subject_id
        if active_subject is None:
            raise MemoryAuthorityDenied("memory access requires an active subject")

        member_ids = {
            manifest.account_owner_id,
            *(role.person_id for role in manifest.roles),
        }
        registered = active_subject in member_ids
        if not registered:
            raise MemoryAuthorityDenied(
                "unregistered or guest speakers cannot access durable memory"
            )

        roles = tuple(
            cast(BindingRoleValue, role)
            for role in _ROLE_PRIORITY
            if any(
                item.person_id == actor_id and item.role == role
                for item in manifest.roles
            )
        )
        if not roles:
            raise MemoryAuthorityDenied("actor has no active binding role")
        binding_role = _select_binding_role(
            roles,
            actor_id=actor_id,
            active_subject_id=active_subject,
        )
        fence = WriteFence(
            session_id=profile.session_id,
            epoch=profile.session_epoch,
            binding_id=profile.binding_id,
            binding_role=binding_role,
            runtime_profile_id=profile.runtime_profile_id,
            actor_subject_id=actor_id,
            active_subject_id=active_subject,
            binding_version=profile.binding_version,
            device_id=profile.device_id,
            subject_revision=profile.subject_revision,
            family_space_id=manifest.family_space_id,
            generation_id=str(context.generation_id),
            turn_id=context.turn_id,
            valid_until=profile.expires_at,
        )
        return MemoryAuthoritySnapshot(
            fence=fence,
            active_subject_id=active_subject,
            subject_category=cast(
                SubjectCategoryValue,
                _canonical_profile_value(profile.subject_category),
            ),
            age_band=cast(
                AgeBandValue,
                _canonical_profile_value(profile.age_band),
            ),
            speaker_state=cast(
                SpeakerStateValue,
                _canonical_profile_value(profile.speaker_state),
            ),
            speaker_confidence=profile.speaker_confidence,
            registered=registered,
            actor_binding_role=binding_role,
            actor_binding_roles=roles,
            family_space_id=manifest.family_space_id,
            consent_snapshot_id=manifest.consent_snapshot_id,
            profile_revision=context.profile_revision,
            generation_id=context.generation_id,
            turn_id=context.turn_id,
            tool_epoch=context.tool_epoch,
            policy_bundle_version=profile.policy_bundle_version,
            policy_receipt_ids=profile.policy_receipt_ids,
            service_mode=cast(
                ServiceModeValue,
                _canonical_profile_value(profile.service_mode),
            ),
        )

    async def current_consent_snapshot_id(
        self,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> str | None:
        """Keep the unscoped consent lookup fail closed.

        A consent id is authoritative only inside the Session transaction
        that also supplies the actor/session fence.  This adapter's public
        ``current`` operation has that fence; this legacy subject-only port
        does not.  Do not guess a binding or borrow another session here.
        """
        del actor_subject_id, now
        raise MemoryAuthorityUnavailable(
            "current consent requires a session-scoped authority snapshot"
        )


def _canonical_profile_value(value: object) -> str:
    """Accept generated enum fields and string-compatible test profiles."""
    candidate = getattr(value, "value", value)
    if not isinstance(candidate, str):
        raise MemoryAuthorityUnavailable("Runtime Profile enum value is invalid")
    return candidate


def _select_binding_role(
    roles: tuple[BindingRoleValue, ...],
    *,
    actor_id: str,
    active_subject_id: str,
) -> BindingRoleValue:
    if actor_id != active_subject_id and "guardian" in roles:
        return "guardian"
    if "primary_subject" in roles:
        return "primary_subject"
    return roles[0]
